import os
import numpy as np
import torch
import torch.nn as nn
from torch.nn import init
import cv2
import sys
sys.path.append("..")
from networks.sync_batchnorm import SynchronizedBatchNorm2d
from config import cfg
import torch.nn.functional as F
##TODO
###mask_damaging



#############################################################GLOBAL_DIST_MAP
USE_CORRELATION_COST = False
WRONG_LABEL_PADDING_DISTANCE = 1e20
EPSILON=1e-5
if USE_CORRELATION_COST:
#    from correlation_package.correlation import Correlation
    from spatial_correlation_sampler import SpatialCorrelationSampler

def _pairwise_distances(x,y):
    """Computes pairwise squared l2 distances between tensors x and y.
    Args:
    x: Tensor of shape [n, feature_dim].
    y: Tensor of shape [m, feature_dim].
    Returns:
    Float32 distances tensor of shape [n, m].
    """

    xs = torch.sum(x*x,1)
    ys = torch.sum(y*y,1)
    xs = xs.unsqueeze(1)
    ys = ys.unsqueeze(0)
    d = xs+ys -2.*torch.matmul(x,torch.t(y))
    return d

##################
def _flattened_pairwise_distances(reference_embeddings, query_embeddings):
    """Calculates flattened tensor of pairwise distances between ref and query.
    Args:
    reference_embeddings: Tensor of shape [..., embedding_dim],
      the embedding vectors for the reference frame
    query_embeddings: Tensor of shape [n_query_images, height, width,
      embedding_dim], the embedding vectors for the query frames.
    Returns:
    A distance tensor of shape [reference_embeddings.size / embedding_dim,
    query_embeddings.size / embedding_dim]
    """
    embedding_dim = query_embeddings.size()[-1]
    reference_embeddings = reference_embeddings.view(-1, embedding_dim)
    first_dim = -1
    query_embeddings = query_embeddings.view(first_dim,embedding_dim)
    dists = _pairwise_distances(query_embeddings, reference_embeddings)
    return dists


def _nn_features_per_object_for_chunk(
    reference_embeddings, query_embeddings, wrong_label_mask,
    k_nearest_neighbors):
    """Extracts features for each object using nearest neighbor attention.
  Args:
    reference_embeddings: Tensor of shape [n_chunk, embedding_dim],
      the embedding vectors for the reference frame.
    query_embeddings: Tensor of shape [m_chunk, embedding_dim], the embedding
      vectors for the query frames.
    wrong_label_mask:
    k_nearest_neighbors: Integer, the number of nearest neighbors to use.
  Returns:
    nn_features: A float32 tensor of nearest neighbor features of shape
      [m_chunk, n_objects, feature_dim].
    """
    reference_embeddings_key = reference_embeddings
    query_embeddings_key = query_embeddings
    dists = _flattened_pairwise_distances(reference_embeddings_key,query_embeddings_key)
    dists = (torch.unsqueeze(dists,1) +
            torch.unsqueeze(wrong_label_mask.float(),0) *
           WRONG_LABEL_PADDING_DISTANCE)
    if k_nearest_neighbors == 1:
        features,_= torch.min(dists,2,keepdim=True)
    else:
        dists,_=torch.topk(-dists,k=k_nearest_neighbors,dim=2)
        dists = -dists
        valid_mask = (dists<WRONG_LABEL_PADDING_DISTANCE)
        masked_dists = dists * valid_mask.float()
        pad_dist = torch.max(masked_dists, dim=2,keepdim=True)[0].repeat((1,1,masked_dists.size()[-1]))
        dists = torch.where(valid_mask, dists, pad_dist)
         # take mean of distances
        features = torch.mean(dists, dim=2, keepdim=True)


    return features



def _nearest_neighbor_features_per_object_in_chunks(
    reference_embeddings_flat, query_embeddings_flat, reference_labels_flat,
    ref_obj_ids, k_nearest_neighbors, n_chunks):
    """Calculates the nearest neighbor features per object in chunks to save mem.
    Uses chunking to bound the memory use.
    Args:
    reference_embeddings_flat: Tensor of shape [n, embedding_dim],
      the embedding vectors for the reference frame.
    query_embeddings_flat: Tensor of shape [m, embedding_dim], the embedding
      vectors for the query frames.
    reference_labels_flat: Tensor of shape [n], the class labels of the
      reference frame.
    ref_obj_ids: int tensor of unique object ids in the reference labels.
    k_nearest_neighbors: Integer, the number of nearest neighbors to use.
    n_chunks: Integer, the number of chunks to use to save memory
      (set to 1 for no chunking).
    Returns:
    nn_features: A float32 tensor of nearest neighbor features of shape
      [m, n_objects, feature_dim].
    """
    chunk_size = int(np.ceil(float(query_embeddings_flat.size()[0])
                                        / n_chunks))
    wrong_label_mask = (reference_labels_flat!=torch.unsqueeze(ref_obj_ids,1))
    all_features = []
    for n in range(n_chunks):
        if n_chunks == 1:
            query_embeddings_flat_chunk = query_embeddings_flat
        else:
            chunk_start = n * chunk_size
            chunk_end = (n + 1) * chunk_size
            query_embeddings_flat_chunk = query_embeddings_flat[chunk_start:chunk_end]
        features = _nn_features_per_object_for_chunk(
            reference_embeddings_flat, query_embeddings_flat_chunk,
            wrong_label_mask, k_nearest_neighbors
            )
        all_features.append(features)
    if n_chunks == 1:
        nn_features = all_features[0]
    else:
        nn_features = torch.cat(all_features, dim=0)
    return nn_features


def nearest_neighbor_features_per_object(
    reference_embeddings, query_embeddings, reference_labels,
     k_nearest_neighbors, gt_ids=None, n_chunks=100):
    """Calculates the distance to the nearest neighbor per object.
    For every pixel of query_embeddings calculate the distance to the
    nearest neighbor in the (possibly subsampled) reference_embeddings per object.
    Args:
    reference_embeddings: Tensor of shape [height, width, embedding_dim],
      the embedding vectors for the reference frame.
    query_embeddings: Tensor of shape [n_query_images, height, width,
      embedding_dim], the embedding vectors for the query frames.
    reference_labels: Tensor of shape [height, width, 1], the class labels of
      the reference frame.
    max_neighbors_per_object: Integer, the maximum number of candidates
      for the nearest neighbor query per object after subsampling,
      or 0 for no subsampling.
    k_nearest_neighbors: Integer, the number of nearest neighbors to use.
    gt_ids: Int tensor of shape [n_objs] of the sorted unique ground truth
      ids in the first frame. If None, it will be derived from
      reference_labels.
    n_chunks: Integer, the number of chunks to use to save memory
      (set to 1 for no chunking).
    Returns:
    nn_features: A float32 tensor of nearest neighbor features of shape
      [n_query_images, height, width, n_objects, feature_dim].
    gt_ids: An int32 tensor of the unique sorted object ids present
      in the reference labels.
    """
    
    assert (reference_embeddings.size()[:2]==reference_labels.size()[:2])
    h,w,_ =query_embeddings.size()
    reference_labels_flat = reference_labels.view(-1)
    if gt_ids is None:
        ref_obj_ids = torch.unique(reference_labels_flat)[-1]
        ref_obj_ids = np.arange(0,ref_obj_ids.cpu()+1)
        gt_ids = torch.from_numpy(ref_obj_ids)
        gt_ids = gt_ids.int()
        if torch.cuda.is_available():
            gt_ids = gt_ids.cuda()
    else:
#        gt_ids = gt_ids.cpu()
#        gt_ids = np.arange(0,gt_ids+1)
#
#        gt_ids = torch.from_numpy(gt_ids)
#        gt_ids = gt_ids.int()
        gt_ids = torch.arange(0,gt_ids+1).int()
        if torch.cuda.is_available():
            gt_ids = gt_ids.cuda()
    embedding_dim = query_embeddings.size()[-1]
    query_embeddings_flat = query_embeddings.view(-1,embedding_dim)
    reference_embeddings_flat = reference_embeddings.view(-1,embedding_dim)
    nn_features = _nearest_neighbor_features_per_object_in_chunks(
        reference_embeddings_flat, query_embeddings_flat, reference_labels_flat,
        gt_ids, k_nearest_neighbors, n_chunks)
    nn_features_dim = nn_features.size()[-1]
    nn_features_reshape=nn_features.view(1,h,w,gt_ids.size(0),nn_features_dim)
    return nn_features_reshape, gt_ids
########################################################################LOCAL_DIST_MAP
def local_pairwise_distances(x, y, max_distance=9):
    """Computes pairwise squared l2 distances using a local search window.
    Optimized implementation using correlation_cost.
    Args:
    x: Float32 tensor of shape [height, width, feature_dim].
    y: Float32 tensor of shape [height, width, feature_dim].
    max_distance: Integer, the maximum distance in pixel coordinates
      per dimension which is considered to be in the search window.
    Returns:
    Float32 distances tensor of shape
      [height, width, (2 * max_distance + 1) ** 2].
    """
    if cfg.MODEL_LOCAL_DOWNSAMPLE:
            #####
        ori_h,ori_w,_=x.size()
        x = x.permute(2,0,1).unsqueeze(0)

        x = F.avg_pool2d(x,(2,2),(2,2))
        y= y.permute(2,0,1).unsqueeze(0)
        y = F.avg_pool2d(y,(2,2),(2,2))

        x =x.squeeze(0).permute(1,2,0)
        y = y.squeeze(0).permute(1,2,0)
        corr = cross_correlate(x, y, max_distance=max_distance)
        xs = torch.sum(x*x,2,keepdim=True)

        ys = torch.sum(y*y,2,keepdim=True)
        ones_ys = torch.ones_like(ys)
        ys = cross_correlate(ones_ys, ys, max_distance=max_distance)
        d = xs + ys - 2 * corr
    # Boundary should be set to Inf.
        tmp = torch.zeros_like(d)
        boundary = torch.eq(
            cross_correlate(ones_ys, ones_ys, max_distance=max_distance), 0)
        d = torch.where(boundary, tmp.fill_(float('inf')),d)
        d = (torch.sigmoid(d)-0.5)*2
        d = d.permute(2,0,1).unsqueeze(0)
        d = F.interpolate(d,size=(ori_h,ori_w),mode='bilinear',align_corners=True)
        d = d.squeeze(0).permute(1,2,0)

    #####

    else:
        corr = cross_correlate(x, y, max_distance=max_distance)
        xs = torch.sum(x*x,2,keepdim=True)

        ys = torch.sum(y*y,2,keepdim=True)
        ones_ys = torch.ones_like(ys)
        ys = cross_correlate(ones_ys, ys, max_distance=max_distance)
        d = xs + ys - 2 * corr
        # Boundary should be set to Inf.
        tmp = torch.zeros_like(d)
        boundary = torch.eq(
            cross_correlate(ones_ys, ones_ys, max_distance=max_distance), 0)
        d = torch.where(boundary, tmp.fill_(float('inf')),d)
    return d
def local_pairwise_distances2(x, y, max_distance=9):
    """Computes pairwise squared l2 distances using a local search window.
    Naive implementation using map_fn.
    Used as a slow fallback for when correlation_cost is not available.
    Args:
    x: Float32 tensor of shape [height, width, feature_dim].
    y: Float32 tensor of shape [height, width, feature_dim].
    max_distance: Integer, the maximum distance in pixel coordinates
      per dimension which is considered to be in the search window.
    Returns:
    Float32 distances tensor of shape
      [height, width, (2 * max_distance + 1) ** 2].
    """
    if cfg.MODEL_LOCAL_DOWNSAMPLE:
        ori_h,ori_w,_ = x.size()
        x = x.permute(2,0,1).unsqueeze(0)
        x = F.avg_pool2d(x,(2,2),(2,2))
        y = y.permute(2,0,1).unsqueeze(0)
        y = F.avg_pool2d(y,(2,2),(2,2))

        _,channels,height,width=x.size()
        padding_val=1e20
        padded_y = F.pad(y, (max_distance, max_distance,
            max_distance, max_distance), mode='constant', value=padding_val)
        offset_y = F.unfold(padded_y, kernel_size=(height, width)).view(1, channels, height, width, -1)
        x = x.view(1, channels, height, width, 1)
        minus = x - offset_y
        dists = torch.sum(torch.mul(minus, minus), dim=1).view(1, height, width, -1).permute(0, 3, 1, 2)
        dists = (torch.sigmoid(dists) - 0.5) * 2
        dists = F.interpolate(dists, size=(ori_h, ori_w), mode='bilinear', align_corners=True)
        dists = dists.squeeze(0).permute(1, 2, 0)


    else:
        padding_val = 1e20
        padded_y =nn.functional.pad(y,(0,0,max_distance, max_distance,
                              max_distance, max_distance),mode='constant', value=padding_val)
        height, width, _ = x.size()
        dists = []
        for y_start in range(2 * max_distance + 1):
            y_end = y_start + height
            y_slice = padded_y[y_start:y_end]
            for x_start in range(2 * max_distance + 1):
                x_end = x_start + width
                offset_y = y_slice[:, x_start:x_end]
                dist = torch.sum(torch.pow((x-offset_y),2), dim=2)
                dists.append(dist)
        dists = torch.stack(dists, dim=2)
    return dists


def cross_correlate(x, y, max_distance=9):
    """Efficiently computes the cross correlation of x and y.
  Optimized implementation using correlation_cost.
  Note that we do not normalize by the feature dimension.
  Args:
    x: Float32 tensor of shape [height, width, feature_dim].
    y: Float32 tensor of shape [height, width, feature_dim].
    max_distance: Integer, the maximum distance in pixel coordinates
      per dimension which is considered to be in the search window.
  Returns:
    Float32 tensor of shape [height, width, (2 * max_distance + 1) ** 2].
    """
    #corr_op=orrelation(pad_size=max_distance, kernel_size=1, max_displacement=max_distance, stride1=1, stride2=1, corr_multiply=1)
    corr_op=SpatialCorrelationSampler(kernel_size=1, patch_size=2*max_distance+1, stride=1,dilation_patch=1,padding=0)

    xs = x.permute(2,0,1)
    xs = torch.unsqueeze(xs,0)
    ys = y.permute(2,0,1)
    ys = torch.unsqueeze(ys,0)
    corr = corr_op(xs,ys)
    bs,_,_,hh,ww = corr.size()
    corr = corr.view(bs,-1,hh,ww)
    corr = torch.squeeze(corr,0)
    corr = corr.permute(1,2,0)
#    feature_dim=x.size()[-1]
#    corr *= feature_dim
    return corr



def local_previous_frame_nearest_neighbor_features_per_object(
    prev_frame_embedding, query_embedding, prev_frame_labels,
      gt_ids, max_distance=15):
    """Computes nearest neighbor features while only allowing local matches.
  Args:
    prev_frame_embedding: Tensor of shape [height, width, embedding_dim],
      the embedding vectors for the last frame.
    query_embedding: Tensor of shape [height, width, embedding_dim],
      the embedding vectors for the query frames.
    prev_frame_labels: Tensor of shape [height, width, 1], the class labels of
      the previous frame.
    gt_ids: Int Tensor of shape [n_objs] of the sorted unique ground truth
      ids in the first frame.
    max_distance: Integer, the maximum distance allowed for local matching.
  Returns:
    nn_features: A float32 np.array of nearest neighbor features of shape
      [1, height, width, n_objects, 1].
    """
    if USE_CORRELATION_COST:
        d = local_pairwise_distances(query_embedding, prev_frame_embedding,
                                   max_distance=max_distance)
    else:
        d = local_pairwise_distances2(query_embedding, prev_frame_embedding,
                                    max_distance=max_distance)
#    d = (torch.sigmoid(d) - 0.5) * 2
    height,width=prev_frame_embedding.size()[:2]

    if USE_CORRELATION_COST:
      #  corr_op=Correlation(pad_size=max_distance, kernel_size=1, max_displacement=max_distance, stride1=1, stride2=1, corr_multiply=1)
        corr_op=SpatialCorrelationSampler(kernel_size=1, patch_size=2*max_distance+1,stride=1,dilation_patch=1,padding=0)
        # New, faster code with cross-correlation via correlation_cost.
      # Due to padding we have to add 1 to the labels.
        tmp_prev_frame_labels=(prev_frame_labels+1).float().permute(2,0,1)
        tmp_prev_frame_labels=torch.unsqueeze(tmp_prev_frame_labels,0)
        
        ones_ = torch.ones_like(tmp_prev_frame_labels)
        offset_labels = corr_op(ones_,tmp_prev_frame_labels)
        bs,_,_,hh,ww=offset_labels.size()
        offset_labels=offset_labels.view(bs,-1,hh,ww)



        offset_labels = torch.squeeze(offset_labels,0)
        offset_labels = offset_labels.permute(1,2,0)
        offset_labels = torch.unsqueeze(offset_labels,3)
        offset_labels = torch.round(offset_labels - 1)
        offset_masks = torch.eq(
          offset_labels,
          gt_ids.float().unsqueeze(0).unsqueeze(0).unsqueeze(0))


    else:
        masks = torch.eq(prev_frame_labels, gt_ids.unsqueeze(0).unsqueeze(0))
        padded_masks = nn.functional.pad(masks,
                            (0,0,max_distance, max_distance,
                             max_distance, max_distance,
                             ))
        offset_masks = []
        for y_start in range(2 * max_distance + 1):
            y_end = y_start + height
            masks_slice = padded_masks[y_start:y_end]
            for x_start in range(2 * max_distance + 1):
                x_end = x_start + width
                offset_mask = masks_slice[:, x_start:x_end]
                offset_masks.append(offset_mask)
        offset_masks = torch.stack(offset_masks, dim=2)
#    pad = torch.ones((height, width, (2 * max_distance + 1) ** 2, gt_ids.size(0)))
    d_tiled = d.unsqueeze(-1).repeat((1,1,1,gt_ids.size(0)))
    pad = torch.ones_like(d_tiled)
#    if torch.cuda.is_available():
#        pad=pad.cuda()
#        d_tiled = d_tiled.cuda()
    d_masked = torch.where(offset_masks, d_tiled, pad)
    dists,_ = torch.min(d_masked, dim=2)
    dists = dists.view(1, height, width, gt_ids.size(0), 1)
    return dists
    
##############################################################
class LocalRefine(nn.Module):
    def __init__(self,in_dim=(cfg.MODEL_SEMANTIC_EMBEDDING_DIM+1),embed_dim=cfg.MODEL_HEAD_EMBEDDING_DIM):
        super(LocalRefine,self).__init__()
        self.layer1=_split_separable_conv2d(in_dim,embed_dim,5)
        self.layer2=_split_separable_conv2d(embed_dim,embed_dim,3)
        self.conv=nn.Conv2d(embed_dim,1,1,1)
        nn.init.kaiming_normal_(self.conv.weight,mode='fan_out',nonlinearity='relu')
    def forward(self,x):
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.conv(x)
        return x
#############################
class Global_Atten(nn.Module):
    def __init__(self,in_dim=(cfg.MODEL_SEMANTIC_EMBEDDING_DIM+1),out_dim=cfg.MODEL_HEAD_EMBEDDING_DIM,kernel_size=7):
        super(Global_Atten,self).__init__()
        self.conv1 = nn.Conv2d(in_dim,out_dim,kernel_size=kernel_size,stride=1,padding=int((kernel_size-1)/2))
        self.relu1=nn.ReLU(True)
        self.bn1 = SynchronizedBatchNorm2d(out_dim, momentum=cfg.TRAIN_BN_MOM)
        self.conv2 = nn.Conv2d(out_dim,1,kernel_size=1,stride=1)
        nn.init.kaiming_normal_(self.conv1.weight,mode='fan_out',nonlinearity='relu')
        nn.init.kaiming_normal_(self.conv2.weight,mode='fan_out',nonlinearity='relu')
    def forward(self,x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu1(x)
        x = self.conv2(x)
        return x        






class _split_separable_conv2d(nn.Module):
    def __init__(self,in_dim,out_dim,kernel_size=7):
        super(_split_separable_conv2d,self).__init__()
        self.conv1=nn.Conv2d(in_dim,in_dim,kernel_size=kernel_size,stride=1,padding=int((kernel_size-1)/2),groups=in_dim)
        self.relu1=nn.ReLU()
        self.bn1 = SynchronizedBatchNorm2d(in_dim, momentum=cfg.TRAIN_BN_MOM)
        self.conv2=nn.Conv2d(in_dim,out_dim,kernel_size=1,stride=1)
        self.relu2=nn.ReLU()
        self.bn2 = SynchronizedBatchNorm2d(out_dim, momentum=cfg.TRAIN_BN_MOM)
        nn.init.kaiming_normal_(self.conv1.weight,mode='fan_out',nonlinearity='relu')
        nn.init.kaiming_normal_(self.conv2.weight,mode='fan_out',nonlinearity='relu')
    def forward(self,x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu1(x)
        x = self.conv2(x)
        x = self.bn2(x)
        x = self.relu2(x)
        return x


#################
class DynamicSegHead(nn.Module):
    def __init__(self,in_dim=(cfg.MODEL_SEMANTIC_EMBEDDING_DIM+3),embed_dim=cfg.MODEL_HEAD_EMBEDDING_DIM,kernel_size=1,siamese_atten=cfg.MODEL_SIAMESE_ATTETION):
        super(DynamicSegHead,self).__init__()
        self.siamese_atten=siamese_atten
        self.layer1=_split_separable_conv2d(in_dim,embed_dim)
        self.layer2=_split_separable_conv2d(embed_dim,embed_dim)
        self.layer3=_split_separable_conv2d(embed_dim,embed_dim)
        self.layer4=_split_separable_conv2d(embed_dim,embed_dim)

        if self.siamese_atten:
            self.siamese_head=nn.Linear(cfg.MODEL_SEMANTIC_EMBEDDING_DIM , cfg.MODEL_HEAD_EMBEDDING_DIM + 1)
            self.conv=nn.Conv2d(embed_dim+1,1,1,1)
            nn.init.kaiming_normal_(self.conv.weight,mode='fan_out',nonlinearity='relu')
        else:
            self.conv=nn.Conv2d(embed_dim,1,1,1)
            nn.init.kaiming_normal_(self.conv.weight,mode='fan_out',nonlinearity='relu')

        

################TODO
    def forward(self,x,atten_maps=None,s_atten_head=None):
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        if atten_maps is not None:
            x = x*atten_maps
        if self.siamese_atten:
            siam_output = self.siamese_head(s_atten_head)
            siam_weight = siam_output[:, :cfg.MODEL_HEAD_EMBEDDING_DIM]
            siam_bias = siam_output[:, -1]
            n, c, h, w = x.size()
            y = x.view(1, n * c, h, w)
            siam_weight = siam_weight.view(n, c, 1, 1)
            siam_bias = siam_bias.view(-1)
            y = F.conv2d(y, weight=siam_weight, bias=siam_bias, groups=n).view(n, 1, h, w)
            x = torch.cat((x,y),1)
            x = self.conv(x)
        else:

            x = self.conv(x)
        return x


    ###############
#    return current_frame_embedding,global_match_map,local_match_map,scale_previous_frame_label
class IntVOS(nn.Module):
    def __init__(self,cfg,feature_extracter,dynamic_seghead=DynamicSegHead(),freeze_bn=False):
        super(IntVOS,self).__init__()
        self.feature_extracter=feature_extracter
        self.feature_extracter.cls_conv=nn.Sequential()
        self.feature_extracter.upsample4=nn.Sequential()
        self.semantic_embedding=None
        self.seperate_conv=nn.Conv2d(cfg.MODEL_ASPP_OUTDIM,cfg.MODEL_ASPP_OUTDIM,kernel_size=3,stride=1,padding=1,groups=cfg.MODEL_ASPP_OUTDIM)
        self.bn1 = SynchronizedBatchNorm2d(cfg.MODEL_ASPP_OUTDIM, momentum=cfg.TRAIN_BN_MOM)
        self.relu1=nn.ReLU()
        self.embedding_conv = nn.Conv2d(cfg.MODEL_ASPP_OUTDIM,cfg.MODEL_SEMANTIC_EMBEDDING_DIM,1,1)
        self.relu2=nn.ReLU()
        self.bn2  = SynchronizedBatchNorm2d(cfg.MODEL_SEMANTIC_EMBEDDING_DIM, momentum=cfg.TRAIN_BN_MOM)
        self.semantic_embedding=nn.Sequential(*[self.seperate_conv,self.bn1,self.relu1,self.embedding_conv,self.bn2,self.relu2])
#        self.semantic_embedding=nn.Sequential(*[self.seperate_conv,self.relu1,self.embedding_conv,self.relu2])

        for m in self.semantic_embedding:
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')

        
        #########################
        if cfg.MODEL_GLOBAL_ATTEN:
            self.global_atten=Global_Atten()
        self.dynamic_seghead = dynamic_seghead
        self.localrefine = LocalRefine()

        if freeze_bn:
            self.freeze_bn()
    def forward(self,x=None,ref_scribble_label=None,previous_frame_mask=None,normalize_nearest_neighbor_distances=True,seq_names=None,gt_ids=None,k_nearest_neighbors=1):

        x=self.extract_feature(x)
        ref_frame_embedding, previous_frame_embedding,current_frame_embedding = torch.split(x,split_size_or_sections=int(x.size(0)/3), dim=0)

        dic,local_refine_dic = self.before_seghead_process(ref_frame_embedding,previous_frame_embedding,
                    current_frame_embedding,ref_scribble_label,previous_frame_mask,
                    normalize_nearest_neighbor_distances,
                    seq_names,gt_ids,k_nearest_neighbors,self.dynamic_seghead)
        return dic,local_refine_dic



    
    
    def calculate_attention_head(self, ref_embedding, ref_label):
        ref_head = ref_embedding * ref_label
        ref_head = torch.sum(ref_head, dim=(2,3)) / (torch.sum(ref_label, dim=(2,3)) + EPSILON)


        
        return ref_head





    def extract_feature(self,x):
        x=self.feature_extracter(x)
        x=self.semantic_embedding(x)
        return x
    
#    def pred_label(self,x):
    def before_seghead_process(self,ref_frame_embedding=None,previous_frame_embedding=None, current_frame_embedding=None,
                ref_scribble_label=None,previous_frame_mask=None,normalize_nearest_neighbor_distances=True,
                seq_names=None,gt_ids=None,k_nearest_neighbors=1,dynamic_seghead=None):

        """return: feature_embedding,global_match_map,local_match_map,previous_frame_mask"""
    ###############
        dic_tmp={}
        local_refine_dic={}
        bs,c,h,w = current_frame_embedding.size()
        scale_ref_scribble_label=torch.nn.functional.interpolate(ref_scribble_label.float(),size=(h,w),mode='nearest')
        scale_ref_scribble_label = scale_ref_scribble_label.int()
        scale_previous_frame_label=torch.nn.functional.interpolate(previous_frame_mask.float(),size=(h,w),mode='nearest')
        scale_previous_frame_label=scale_previous_frame_label.int()
        #ref_frame_embedding = ref_frame_embedding.detach()
        #previous_frame_embedding=previous_frame_embedding.detach()
        if USE_CORRELATION_COST:
            n_chunks = 20
        else:
            n_chunks = 500
    
        for n in range(bs):
            seq_current_frame_embedding = current_frame_embedding[n]
            seq_ref_frame_embedding = ref_frame_embedding[n]
            seq_prev_frame_embedding = previous_frame_embedding[n]
            ########################Global dist map
            seq_ref_frame_embedding = seq_ref_frame_embedding.permute(1,2,0)
            seq_current_frame_embedding = seq_current_frame_embedding.permute(1,2,0)
            seq_ref_scribble_label = scale_ref_scribble_label[n].permute(1,2,0)
            nn_features_n, ref_obj_ids = nearest_neighbor_features_per_object(reference_embeddings=seq_ref_frame_embedding, query_embeddings=seq_current_frame_embedding, 
                reference_labels=seq_ref_scribble_label, k_nearest_neighbors=k_nearest_neighbors, gt_ids=gt_ids[n], n_chunks=20)
            if normalize_nearest_neighbor_distances:
                nn_features_n = (torch.sigmoid(nn_features_n) - 0.5) * 2
            #########################TODO
#            if is_training and damage_initial_previous_frame_mask:
                # Damage the previous frame masks.
###                                                          dilate=False)
    
    
            #########################Local dist map
            seq_prev_frame_embedding = seq_prev_frame_embedding.permute(1,2,0)
            seq_previous_frame_label = scale_previous_frame_label[n].permute(1,2,0)
            prev_frame_nn_features_n = local_previous_frame_nearest_neighbor_features_per_object(prev_frame_embedding=seq_prev_frame_embedding, 
                            query_embedding=seq_current_frame_embedding, prev_frame_labels=seq_previous_frame_label,
                                gt_ids=ref_obj_ids, max_distance=cfg.MODEL_MAX_LOCAL_DISTANCE)
    
           # #########################
            previous_frame_to_cat = (seq_previous_frame_label.float()== ref_obj_ids.float())
    
            to_cat_current_frame_embedding = current_frame_embedding[n].unsqueeze(0).repeat((ref_obj_ids.size(0),1,1,1))
    
            to_cat_nn_feature_n = nn_features_n.squeeze(0).permute(2,3,0,1)
            to_cat_previous_frame = previous_frame_to_cat.unsqueeze(-1).permute(2,3,0,1).float()
            to_cat_prev_frame_nn_feature_n = prev_frame_nn_features_n.squeeze(0).permute(2,3,0,1)
            ###
            ref_frame_to_cat = (seq_ref_scribble_label.float() == ref_obj_ids.float())
            to_cat_ref_frame = ref_frame_to_cat.unsqueeze(-1).permute(2,3,0,1).float()
            ###
            if cfg.MODEL_SIAMESE_ATTETION:
                attention_head = self.calculate_attention_head(ref_frame_embedding[n].unsqueeze(0).expand((ref_obj_ids.size(0),-1,-1,-1)),
                     to_cat_ref_frame)
            else:
                attention_head = None
            
            to_refine = torch.cat((to_cat_current_frame_embedding,to_cat_prev_frame_nn_feature_n),1)
            to_cat_refined_prev_frame_nn_feature_n=self.localrefine(to_refine)
            local_refine_dic[seq_names[n]]=to_cat_refined_prev_frame_nn_feature_n.permute(1,0,2,3)
            
            to_cat = torch.cat((to_cat_current_frame_embedding,to_cat_nn_feature_n,to_cat_refined_prev_frame_nn_feature_n,to_cat_previous_frame),1)
         #   to_cat = torch.cat((to_cat_current_frame_embedding,to_cat_nn_feature_n,to_cat_prev_frame_nn_feature_n),1)
          #  to_cat = torch.cat((to_cat_current_frame_embedding,to_cat_nn_feature_n),1)
          
          
            if cfg.MODEL_GLOBAL_ATTEN:
                cat_global_=torch.cat((to_cat_current_frame_embedding,to_cat_nn_feature_n),1)
                atten_maps = self.global_atten(cat_global_)
                atten_maps = torch.nn.functional.softmax(atten_maps,0)
                pred_=dynamic_seghead(to_cat,atten_maps)
            elif cfg.MODEL_SIAMESE_ATTETION:
                pred_ = dynamic_seghead(to_cat,s_atten_head=attention_head)
            else:
                pred_=dynamic_seghead(to_cat)
#            pred_=dynamic_seghead(to_cat)
            pred_ = pred_.permute(1,0,2,3)
            dic_tmp[seq_names[n]]=pred_
        return dic_tmp,local_refine_dic
    def freeze_bn(self):
        for m in self.modules():
            if isinstance(m, SynchronizedBatchNorm2d):
                m.eval()
            elif isinstance(m, nn.BatchNorm2d):
                m.eval()

