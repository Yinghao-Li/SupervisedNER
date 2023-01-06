# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import torch
from .dataset import DataInstance, instance_list_to_feature_lists
from typing import List


class Batch:
    def __init__(self, **kwargs):
        super().__init__()
        for k, v in kwargs.items():
            setattr(self, k, v)

    def to(self, device):
        for k, v in self.__dict__.items():
            try:
                setattr(self, k, v.to(device))
            except AttributeError:
                pass
        return self

    def __len__(self):
        for v in list(self.__dict__.values()):
            if isinstance(v, torch.Tensor):
                return v.size(0)


def collator(instance_list: List[DataInstance]):

    embs, lbs = instance_list_to_feature_lists(instance_list, ['embs', 'lbs'])

    seq_lengths = [f.size(0) for f in embs]
    max_length = max(seq_lengths)
    feature_dim = embs[0].size(-1)

    txt_emb_batch = torch.stack([
        torch.cat((tk_embs, torch.zeros(max_length - len(tk_embs), feature_dim)), dim=0) for tk_embs in embs
    ])
    lbs_batch = torch.stack([
        torch.cat((lb, torch.full((max_length - len(lb), ), -1)), dim=0) for lb in lbs
    ])

    padding_mask_batch = lbs_batch == -1

    return Batch(
        txt_emb=txt_emb_batch,
        lbs=lbs_batch,
        padding_mask=padding_mask_batch,
    )
