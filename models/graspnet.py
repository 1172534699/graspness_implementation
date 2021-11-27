""" GraspNet baseline model definition.
    Author: chenxi-wang
"""

import os
import sys
import numpy as np
import torch
import torch.nn as nn
import MinkowskiEngine as ME

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.append(ROOT_DIR)

from models.backbone_resunet14 import MinkUNet14
from models.modules import ApproachNet, GraspableNet, CloudCrop, SWADNet
from loss_utils import GRASP_MAX_WIDTH, NUM_VIEW, NUM_ANGLE, NUM_DEPTH, GRASPNESS_THRESHOLD, M_POINT
from label_generation import process_grasp_labels, match_grasp_view_and_label, batch_viewpoint_params_to_matrix


class GraspNet(nn.Module):
    def __init__(self, cylinder_radius=0.05, seed_feat_dim=512, is_training=True, log_string=None):
        super().__init__()
        self.is_training = is_training
        self.log_string = log_string
        self.seed_feature_dim = seed_feat_dim
        self.num_depth = NUM_DEPTH
        self.num_angle = NUM_ANGLE
        self.M_points = M_POINT
        self.num_view = NUM_VIEW

        self.backbone = MinkUNet14(in_channels=3, out_channels=self.seed_feature_dim, D=3)
        self.graspable = GraspableNet(seed_feature_dim=self.seed_feature_dim)
        self.rotation = ApproachNet(self.num_view, seed_feature_dim=self.seed_feature_dim, is_training=self.is_training)
        self.crop = CloudCrop(nsample=16, cylinder_radius=cylinder_radius, seed_feature_dim=self.seed_feature_dim)
        self.swd = SWADNet(num_angle=self.num_angle, num_depth=self.num_depth)

    def forward(self, end_points):
        seed_xyz = end_points['point_clouds']  # use all sampled point cloud
        B, point_num, _ = seed_xyz.shape  # batch _size
        # point-wise features
        coordinates_batch = end_points['coors']
        features_batch = end_points['feats']
        mink_input = ME.SparseTensor(features_batch, coordinates=coordinates_batch)
        seed_features = self.backbone(mink_input).F
        seed_features = seed_features[end_points['quantize2original']].view(B, point_num, -1).transpose(1, 2)

        end_points = self.graspable(seed_features, end_points)
        seed_features_flipped = seed_features.transpose(1, 2)
        objectness_score = end_points['objectness_score']
        graspness_score = end_points['graspness_score'].squeeze(1)
        objectness_pred = torch.argmax(objectness_score, 1)
        objectness_mask = (objectness_pred == 1)
        graspness_mask = graspness_score > GRASPNESS_THRESHOLD
        graspable_mask = objectness_mask & graspness_mask

        seed_features_graspable = []
        seed_xyz_graspable = []
        # seed_inds_graspable = []
        graspable_num_batch = 0.
        for i in range(B):
            cur_mask = graspable_mask[i]
            # inds = torch.arange(point_num).to(objectness_score.device)
            graspable_num = cur_mask.sum()
            graspable_num_batch += graspable_num
            if graspable_num < 200:
                if self.log_string is None:
                    print('Warning!!! Two few graspable points! only {}'.format(graspable_num))
                else:
                    self.log_string('Warning!!! Two few graspable points! only {}'.format(graspable_num))
                cur_mask_danger = cur_mask.detach().clone()
                cur_mask_danger[:800] = True
                graspable_num = cur_mask_danger.sum()
                cur_feat = seed_features_flipped[i][cur_mask_danger]
                cur_seed_xyz = seed_xyz[i][cur_mask_danger]
                # cur_inds = inds[cur_mask_danger]
            else:
                cur_feat = seed_features_flipped[i][cur_mask]
                cur_seed_xyz = seed_xyz[i][cur_mask]
                # cur_inds = inds[cur_mask]

            if graspable_num >= self.M_points:
                idxs = torch.multinomial(torch.ones(graspable_num), self.M_points, replacement=False)
            else:
                idxs1 = torch.arange(graspable_num)
                idxs2 = torch.multinomial(torch.ones(graspable_num), self.M_points - graspable_num, replacement=True)
                idxs = torch.cat([idxs1, idxs2])

            cur_feat = cur_feat[idxs]
            cur_seed_xyz = cur_seed_xyz[idxs]
            # cur_inds = cur_inds[idxs]
            seed_features_graspable.append(cur_feat)
            seed_xyz_graspable.append(cur_seed_xyz)
            # seed_inds_graspable.append(cur_inds)
        seed_xyz_graspable = torch.stack(seed_xyz_graspable, 0)
        # seed_inds_graspable = torch.stack(seed_inds_graspable, 0)
        seed_features_graspable = torch.stack(seed_features_graspable)
        seed_features_graspable = seed_features_graspable.transpose(1, 2)
        end_points['xyz_graspable'] = seed_xyz_graspable
        # end_points['inds_graspable'] = seed_inds_graspable
        end_points['graspable_count_stage1'] = graspable_num_batch / B

        # end_points, res_feat = self.rotation(seed_features_graspable, end_points)
        # seed_features_graspable = seed_features_graspable + res_feat  # residual feat from view selection
        end_points = self.rotation(seed_features_graspable, end_points)

        if self.is_training:
            end_points = process_grasp_labels(end_points)
            grasp_top_views_rot, end_points = match_grasp_view_and_label(end_points)
        else:
            grasp_top_views_rot = end_points['grasp_top_view_rot']

        seed_features_graspable = seed_features_graspable.contiguous()
        seed_xyz_graspable = seed_xyz_graspable.contiguous()
        group_features = self.crop(seed_xyz_graspable, seed_features_graspable, grasp_top_views_rot)
        end_points = self.swd(group_features, end_points)

        return end_points


def pred_decode(end_points):
    batch_size = len(end_points['point_clouds'])
    grasp_preds = []
    for i in range(batch_size):
        grasp_center = end_points['xyz_graspable'][i].float()

        grasp_score = end_points['grasp_score_pred'][i].float()
        grasp_score = grasp_score.view(M_POINT, NUM_ANGLE*NUM_DEPTH)
        grasp_score, grasp_score_inds = torch.max(grasp_score, -1)  # [M_POINT]
        grasp_score = grasp_score.view(-1, 1)
        grasp_angle = (grasp_score_inds // NUM_DEPTH) * np.pi / 12
        grasp_depth = (grasp_score_inds % NUM_DEPTH + 1) * 0.01
        grasp_depth = grasp_depth.view(-1, 1)
        grasp_width = 1.2 * end_points['grasp_width_pred'][i] / 10.  # grasp width gt has been multiplied by 10
        grasp_width = grasp_width.view(M_POINT, NUM_ANGLE*NUM_DEPTH)
        grasp_width = torch.gather(grasp_width, 1, grasp_score_inds.view(-1, 1))
        grasp_width = torch.clamp(grasp_width, min=0., max=GRASP_MAX_WIDTH)

        approaching = -end_points['grasp_top_view_xyz'][i].float()
        grasp_rot = batch_viewpoint_params_to_matrix(approaching, grasp_angle)
        grasp_rot = grasp_rot.view(M_POINT, 9)

        # merge preds
        grasp_height = 0.02 * torch.ones_like(grasp_score)
        obj_ids = -1 * torch.ones_like(grasp_score)
        grasp_preds.append(
            torch.cat([grasp_score, grasp_width, grasp_height, grasp_depth, grasp_rot, grasp_center, obj_ids], axis=-1))
    return grasp_preds