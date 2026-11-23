"""Trainable policy architectures for the optional numerical ML stack."""

from __future__ import annotations

from typing import Any, ClassVar, TYPE_CHECKING

from sb3_contrib.common.maskable.policies import MaskableActorCriticPolicy
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from torch import cat, nn

from fairy.agents.models.numeric.irrigation_window import (
    HISTORY_DAYS,
    HISTORY_FIELDS,
    SNAPSHOT_FIELDS,
)

if TYPE_CHECKING:
    from torch import Tensor
    from gymnasium.spaces import Space
    from stable_baselines3.common.type_aliases import Schedule


class IrrigationActorCriticPolicy(MaskableActorCriticPolicy):
    """Two independent MLP branches over the same flat public feature vector.

    The actor outputs one logit per discrete irrigation dose before action
    masking. The critic outputs a scalar state value used during PPO training.
    Input and head dimensions come from the environment's Gymnasium spaces.
    """

    actor_hidden_units: ClassVar[tuple[int, ...]] = (32, 32)
    critic_hidden_units: ClassVar[tuple[int, ...]] = (32, 32)

    def __init__(
        self,
        observation_space: Space,
        action_space: Space,
        lr_schedule: Schedule,
        **policy_kwargs: Any,
    ) -> None:
        """Set architecture defaults while honoring SB3's saved constructor arguments."""
        policy_kwargs.setdefault(
            "net_arch",
            {"pi": list(self.actor_hidden_units), "vf": list(self.critic_hidden_units)},
        )
        policy_kwargs.setdefault("activation_fn", nn.Tanh)
        super().__init__(observation_space, action_space, lr_schedule, **policy_kwargs)


class SoilWaterStressRegressor(nn.Module):
    """Predict past-day water-deficit severity from an ordered public sensor window."""

    hidden_units: ClassVar[int] = 16

    def __init__(self, input_size: int) -> None:
        super().__init__()
        self.encoder = nn.GRU(input_size, self.hidden_units, batch_first=True)
        self.head = nn.Sequential(nn.Linear(self.hidden_units, 1), nn.Sigmoid())

    def forward(self, observations: Tensor) -> Tensor:
        _, state = self.encoder(observations)
        return self.head(state[0]).squeeze(-1)


class IrrigationWindowEncoder(BaseFeaturesExtractor):
    """Encode the same seven public daily rows supplied to the flat policy.

    A small temporal convolution trades no input fields for its temporal inductive
    bias. Keeping the original feature width makes the actor and critic identical
    in size to the flat-window policy.
    """

    def __init__(self, observation_space: Space) -> None:
        snapshot = len(SNAPSHOT_FIELDS)
        channels = len(HISTORY_FIELDS)
        width = snapshot + HISTORY_DAYS * channels
        if observation_space.shape != (width,):
            raise ValueError("Unexpected irrigation history layout")
        super().__init__(observation_space, features_dim=width)
        self.snapshot_width = snapshot
        self.channels = channels
        self.temporal = nn.Sequential(
            nn.Conv1d(channels, channels, kernel_size=3, padding=1), nn.Tanh()
        )

    def forward(self, observations: Tensor) -> Tensor:
        current = observations[:, : self.snapshot_width]
        window = observations[:, self.snapshot_width :].reshape(
            -1, HISTORY_DAYS, self.channels
        )
        encoded = self.temporal(window.transpose(1, 2)).transpose(1, 2)
        present = window[:, :, HISTORY_FIELDS.index("day_mask")].unsqueeze(-1)
        return cat((current, (encoded * present).flatten(start_dim=1)), dim=1)
