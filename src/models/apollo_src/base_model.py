"""Apollo BaseModel — vendored & slimmed for inference-only use.

Source: JusperLee/Apollo (https://github.com/JusperLee/Apollo), look2hear/models/base_model.py
License: CC-BY-SA 4.0 (Look2Hear, Tsinghua University)

Slimmed for VentiPlayer: removed the huggingface_hub PyTorchModelHubMixin base
and the pytorch_lightning-dependent serialize() path (training/sharing only).
Only the constructor + a self-contained from_pretrain() are kept for inference.
"""

import torch
import torch.nn as nn


class BaseModel(nn.Module):
    def __init__(self, sample_rate, in_chan=1):
        super().__init__()
        self._sample_rate = sample_rate
        self._in_chan = in_chan

    def forward(self, *args, **kwargs):
        raise NotImplementedError

    def sample_rate(self):
        return self._sample_rate

    @classmethod
    def from_pretrain(cls, pretrained_model_conf_or_path, *args, **kwargs):
        """Instantiate the concrete model subclass and load its state dict.

        The Apollo checkpoint stores a dict with keys 'model_name' / 'state_dict'.
        We instantiate the *calling* subclass (Apollo) directly with the given
        model args, then load weights — no model registry needed.
        """
        conf = torch.load(
            pretrained_model_conf_or_path, map_location="cpu", weights_only=False
        )
        state_dict = conf["state_dict"] if isinstance(conf, dict) and "state_dict" in conf else conf
        model = cls(*args, **kwargs)
        model.load_state_dict(state_dict)
        return model

    def get_model_args(self):
        raise NotImplementedError
