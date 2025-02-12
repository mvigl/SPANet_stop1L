from typing import Tuple, Dict, List

import numpy as np
import torch
from torch import Tensor
from torch.nn import functional as F

from spanet.options import Options
from spanet.dataset.types import Batch, Source, AssignmentTargets
from spanet.dataset.regressions import regression_loss
from spanet.network.jet_reconstruction.jet_reconstruction_network import JetReconstructionNetwork
from spanet.network.utilities.divergence_losses import assignment_cross_entropy_loss, jensen_shannon_divergence


def numpy_tensor_array(tensor_list):
    output = np.empty(len(tensor_list), dtype=object)
    output[:] = tensor_list

    return output


class JetReconstructionTraining(JetReconstructionNetwork):
    def __init__(self, options: Options, torch_script: bool = False):
        super(JetReconstructionTraining, self).__init__(options, torch_script)

        self.log_clip = torch.log(10 * torch.scalar_tensor(torch.finfo(torch.float32).eps)).item()

        self.event_particle_names = list(self.training_dataset.event_info.product_particles.keys())
        self.product_particle_names = {
            particle: self.training_dataset.event_info.product_particles[particle][0]
            for particle in self.event_particle_names
        }

    def particle_symmetric_loss(self, assignment: Tensor, detection: Tensor, target: Tensor, mask: Tensor) -> Tensor:
        assignment_loss = assignment_cross_entropy_loss(assignment, target, mask, self.options.focal_gamma)
        detection_loss = F.binary_cross_entropy_with_logits(detection, mask.float(), reduction='none')

        return torch.stack((
            self.options.assignment_loss_scale * assignment_loss,
            self.options.detection_loss_scale * detection_loss
        ))

    def compute_symmetric_losses(self, assignments: List[Tensor], detections: List[Tensor], targets):
        symmetric_losses = []

        # TODO think of a way to avoid this memory transfer but keep permutation indices synced with checkpoint
        # Compute a separate loss term for every possible target permutation.
        for permutation in self.event_permutation_tensor.cpu().numpy():

            # Find the assignment loss for each particle in this permutation.
            current_permutation_loss = tuple(
                self.particle_symmetric_loss(assignment, detection, target, mask)
                for assignment, detection, (target, mask)
                in zip(assignments, detections, targets[permutation])
            )

            # The loss for a single permutation is the sum of particle losses.
            symmetric_losses.append(torch.stack(current_permutation_loss))

        # Shape: (NUM_PERMUTATIONS, NUM_PARTICLES, 2, BATCH_SIZE)
        return torch.stack(symmetric_losses)

    def combine_symmetric_losses(self, symmetric_losses: Tensor) -> Tuple[Tensor, Tensor]:
        # Default option is to find the minimum loss term of the symmetric options.
        # We also store which permutation we used to achieve that minimal loss.
        # combined_loss, _ = symmetric_losses.min(0)
        total_symmetric_loss = symmetric_losses.sum((1, 2))
        index = total_symmetric_loss.argmin(0)

        combined_loss = torch.gather(symmetric_losses, 0, index.expand_as(symmetric_losses))[0]

        # Simple average of all losses as a baseline.
        if self.options.combine_pair_loss.lower() == "mean":
            combined_loss = symmetric_losses.mean(0)

        # Soft minimum function to smoothly fuse all loss function weighted by their size.
        if self.options.combine_pair_loss.lower() == "softmin":
            weights = F.softmin(total_symmetric_loss, 0)
            weights = weights.unsqueeze(1).unsqueeze(1)
            combined_loss = (weights * symmetric_losses).sum(0)

        return combined_loss, index

    def symmetric_losses(
        self,
        assignments: List[Tensor],
        detections: List[Tensor],
        targets: Tuple[Tuple[Tensor, Tensor], ...]
    ) -> Tuple[Tensor, Tensor]:
        # We are only going to look at a single prediction points on the distribution for more stable loss calculation
        # We multiply the softmax values by the size of the permutation group to make every target the same
        # regardless of the number of sub-jets in each target particle
        assignments = [prediction + torch.log(torch.scalar_tensor(decoder.num_targets))
                       for prediction, decoder in zip(assignments, self.branch_decoders)]

        # Convert the targets into a numpy array of tensors so we can use fancy indexing from numpy
        targets = numpy_tensor_array(targets)

        # Compute the loss on every valid permutation of the targets
        symmetric_losses = self.compute_symmetric_losses(assignments, detections, targets)

        # Squash the permutation losses into a single value.
        return self.combine_symmetric_losses(symmetric_losses)

    def symmetric_divergence_loss(self, predictions: List[Tensor], masks: Tensor) -> Tensor:
        divergence_loss = []

        for i, j in self.event_info.event_transpositions:
            # Symmetric divergence between these two distributions
            div = jensen_shannon_divergence(predictions[i], predictions[j])

            # ERF term for loss
            loss = torch.exp(-(div ** 2))
            loss = loss.masked_fill(~masks[i], 0.0)
            loss = loss.masked_fill(~masks[j], 0.0)

            divergence_loss.append(loss)

        return torch.stack(divergence_loss).mean(0)
        # return -1 * torch.stack(divergence_loss).sum(0) / len(self.training_dataset.unordered_event_transpositions)

    def add_kl_loss(
            self,
            total_loss: List[Tensor],
            assignments: List[Tensor],
            masks: Tensor,
            weights: Tensor
    ) -> List[Tensor]:
        if len(self.event_info.event_transpositions) == 0:
            return total_loss

        # Compute the symmetric loss between all valid pairs of distributions.
        kl_loss = self.symmetric_divergence_loss(assignments, masks)
        kl_loss = (weights * kl_loss).sum() / masks.sum()

        with torch.no_grad():
            self.log("loss/symmetric_loss", kl_loss, sync_dist=True)
            if torch.isnan(kl_loss):
                raise ValueError("Symmetric KL Loss has diverged.")

        return total_loss + [self.options.kl_loss_scale * kl_loss]

    def add_regression_loss(
            self,
            total_loss: List[Tensor],
            predictions: Dict[str, Tensor],
            targets:  Dict[str, Tensor]
    ) -> List[Tensor]:
        regression_terms = []

        for key in targets:
            current_target_type = self.training_dataset.regression_types[key]
            current_prediction = predictions[key]
            current_target = targets[key]

            current_mean = self.regression_decoder.networks[key].mean
            current_std = self.regression_decoder.networks[key].std

            current_mask = ~torch.isnan(current_target)

            current_loss = regression_loss(current_target_type)(
                current_prediction[current_mask],
                current_target[current_mask],
                current_mean,
                current_std
            )
            current_loss = torch.mean(current_loss)

            with torch.no_grad():
                self.log(f"loss/regression/{key}", current_loss, sync_dist=True)

            regression_terms.append(self.options.regression_loss_scale * current_loss)

        return total_loss + regression_terms

    def add_classification_loss(
            self,
            total_loss: List[Tensor],
            predictions: Dict[str, Tensor],
            targets: Dict[str, Tensor],
            weights: Dict[str, Tensor]
    ) -> List[Tensor]:
        classification_terms = []

        for key in targets:
            current_prediction = predictions[key]
            current_target = targets[key]

            weight = None if not self.balance_classifications else self.classification_weights[key]
            use_event_weights = True
            if use_event_weights:
                current_loss = F.cross_entropy(
                    current_prediction,
                    current_target,
                    ignore_index=-1,
                    reduction='none'
                )
                # Apply the weights
                device = current_target.device
                #class_weights = torch.ones(len(current_target),device=device)
                #for i,w in enumerate(weight):
                #    class_weights[current_target==i]*=w
                event_weights = weights['EVENT/event_weights']

                weighted_losses = current_loss * event_weights #* class_weights
                # Compute the weighted average loss
                weighted_average_loss = weighted_losses.sum() / (event_weights).sum()

                classification_terms.append(self.options.classification_loss_scale * weighted_average_loss)

                with torch.no_grad():
                    self.log(f"loss/classification/{key}", weighted_average_loss, sync_dist=True)
            else:
                current_loss = F.cross_entropy(
                    current_prediction,
                    current_target,
                    ignore_index=-1,
                    weight=weight
                )
    
                classification_terms.append(self.options.classification_loss_scale * current_loss)
    
                with torch.no_grad():
                    self.log(f"loss/classification/{key}", current_loss, sync_dist=True)

        return total_loss + classification_terms

    def training_step(self, batch: Batch, batch_nb: int) -> Dict[str, Tensor]:
        # ===================================================================================================
        # Parametrize based on mass
        # ---------------------------------------------------------------------------------------------------
        def parametrise(batch):
            tensor_data_2d = batch.sources[1][0][:,:,-2:].squeeze(1)
            unique_rows, counts = torch.unique(tensor_data_2d, dim=0, return_counts=True)
            non_default_mask = unique_rows != (torch.tensor([-1., -1.]).to(unique_rows.device))
            non_default_rows = unique_rows[non_default_mask.any(dim=1)]
            non_default_counts = counts[non_default_mask.any(dim=1)]

            replacements = []
            for row, count in zip(non_default_rows, non_default_counts):
                replacements.extend([row] * count.item())

            if len(replacements) == 0: 
                #masses = [
                #    [500,1],[500,100],[500,200],[500,300],
                #    [600,1],[600,100],[600,200],[600,300],[600,400],[650,450],
                #    [700,1],[700,100],[700,200],[700,300],[700,400],[700,500],[750,550],
                #    [800,1],[800,100],[800,200],[800,300],[800,400],[800,500],[800,600],[850,650],
                #    [900,1],[900,100],[900,200],[900,300],[900,400],[900,500],[900,600],[900,700],
                #    [1000,1],[1000,100],[1000,200],[1000,300],[1000,400],[1000,500],[1000,600],[1000,700],[1000,800],
                #    [1100,1],[1100,100],[1100,200],[1100,300],[1100,400],[1100,500],[1100,600],[1100,700],[1100,800],
                #    [1200,1],[1200,100],[1200,200],[1200,300],[1200,400],[1200,500],[1200,600],[1200,700],[1200,800],
                #    [1300,1],[1300,100],[1300,200],[1300,300],[1300,400],[1300,500],[1300,600],[1300,700],[1300,800],
                #    [1400,1],[1400,100],[1400,200],[1400,300],[1400,400],[1400,500],[1400,600],[1400,700],[1400,800],
                #    [1500,1],[1500,100],[1500,200],[1500,300],[1500,400],[1500,500],[1500,600],[1500,700],[1500,800],
                #    [1600,1],[1600,100],[1600,200],[1600,300],[1600,400],[1600,500],[1600,600],[1600,700],[1600,800]
                #    ]
                masses = [
                    [0,0],[0,1],[0,1],[1,1]
                    ]
                for mass in masses:
                    replacements.extend(torch.tensor(mass))
                    
            while len(replacements) < len(tensor_data_2d):
                replacements.extend(replacements)

            replacements = replacements[:len(tensor_data_2d)]
            default_mask = (tensor_data_2d == (torch.tensor([-1., -1.]).to(unique_rows.device))).all(dim=1)
            default_indices = torch.nonzero(default_mask).flatten()
            np.random.shuffle(replacements)
            for idx in default_indices:
                tensor_data_2d[idx] = replacements.pop()
            batch.sources[1][0][:,:,-2:] = tensor_data_2d.unsqueeze(1)
            return batch
        
        batch = parametrise(batch)
        
        # ===================================================================================================
        # Network Forward Pass
        # ---------------------------------------------------------------------------------------------------
        outputs = self.forward(batch.sources)

        # ===================================================================================================
        # Initial log-likelihood loss for classification task
        # ---------------------------------------------------------------------------------------------------
        symmetric_losses, best_indices = self.symmetric_losses(
            outputs.assignments,
            outputs.detections,
            batch.assignment_targets
        )

        # Construct the newly permuted masks based on the minimal permutation found during NLL loss.
        permutations = self.event_permutation_tensor[best_indices].T
        masks = torch.stack([target.mask for target in batch.assignment_targets])
        masks = torch.gather(masks, 0, permutations)

        # ===================================================================================================
        # Balance the loss based on the distribution of various classes in the dataset.
        # ---------------------------------------------------------------------------------------------------

        # Default unity weight on correct device.
        weights = torch.ones_like(symmetric_losses)

        # Balance based on the particles present - only used in partial event training
        if self.balance_particles:
            class_indices = (masks * self.particle_index_tensor.unsqueeze(1)).sum(0)
            weights *= self.particle_weights_tensor[class_indices]

        # Balance based on the number of jets in this event
        if self.balance_jets:
            weights *= self.jet_weights_tensor[batch.num_vectors]

        # Take the weighted average of the symmetric loss terms.
        masks = masks.unsqueeze(1)
        symmetric_losses = (weights * symmetric_losses).sum(-1) / torch.clamp(masks.sum(-1), 1, None)
        assignment_loss, detection_loss = torch.unbind(symmetric_losses, 1)

        # ===================================================================================================
        # Some basic logging
        # ---------------------------------------------------------------------------------------------------
        with torch.no_grad():
            for name, l in zip(self.training_dataset.assignments, assignment_loss):
                self.log(f"loss/{name}/assignment_loss", l, sync_dist=True)

            for name, l in zip(self.training_dataset.assignments, detection_loss):
                self.log(f"loss/{name}/detection_loss", l, sync_dist=True)

            if torch.isnan(assignment_loss).any():
                raise ValueError("Assignment loss has diverged!")

            if torch.isinf(assignment_loss).any():
                raise ValueError("Assignment targets contain a collision.")

        # ===================================================================================================
        # Start constructing the list of all computed loss terms.
        # ---------------------------------------------------------------------------------------------------
        total_loss = []

        if self.options.assignment_loss_scale > 0:
            total_loss.append(assignment_loss)

        if self.options.detection_loss_scale > 0:
            total_loss.append(detection_loss)

        # ===================================================================================================
        # Auxiliary loss terms which are added to reconstruction loss for alternative targets.
        # ---------------------------------------------------------------------------------------------------
        if self.options.kl_loss_scale > 0:
            total_loss = self.add_kl_loss(total_loss, outputs.assignments, masks, weights)

        if self.options.regression_loss_scale > 0:
            total_loss = self.add_regression_loss(total_loss, outputs.regressions, batch.regression_targets)

        if self.options.classification_loss_scale > 0:
            total_loss = self.add_classification_loss(total_loss, outputs.classifications, batch.classification_targets, batch.event_weights)

        # ===================================================================================================
        # Combine and return the loss
        # ---------------------------------------------------------------------------------------------------
        total_loss = torch.cat([loss.view(-1) for loss in total_loss])

        self.log("loss/total_loss", total_loss.sum(), sync_dist=True)

        return total_loss.mean()
