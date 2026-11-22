"""Trainable policy architectures for the optional numerical ML stack."""

from __future__ import annotations

from typing import Any, ClassVar, TYPE_CHECKING

from sb3_contrib.common.maskable.policies import MaskableActorCriticPolicy
from torch import nn

if TYPE_CHECKING:
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
