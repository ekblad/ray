from typing import Optional, Mapping, Any

import numpy as np
import gymnasium as gym
from gymnasium.spaces import Box, Dict, Discrete, MultiDiscrete, Tuple

from ray.rllib.core.models.base import ModelConfig
from ray.rllib.core.models.base import Encoder
from ray.rllib.core.models.configs import (
    MLPEncoderConfig,
    LSTMEncoderConfig,
    CNNEncoderConfig,
)
from ray.rllib.models import MODEL_DEFAULTS
from ray.rllib.models.tf.tf_distributions import (
    TfCategorical,
    TfDeterministic,
    TfDiagGaussian,
)
from ray.rllib.models.torch.torch_distributions import (
    TorchCategorical,
    TorchDeterministic,
    TorchDiagGaussian,
)
from ray.rllib.models.utils import get_filter_config
from ray.rllib.utils.error import UnsupportedSpaceException
from ray.rllib.utils.spaces.simplex import Simplex


class Catalog:
    """Describes the sub-modules architectures to be used in RLModules.

    RLlib's native RLModules get their Models from a Catalog object.
    By default, that Catalog builds the configs it has as attributes.
    You can modify a Catalog so that it builds different Models by subclassing and
    overriding the build_* methods. Alternatively, you can customize the configs
    inside RLlib's Catalogs to customize what is being built by RLlib.

    Usage example:

    # Define a custom catalog

    .. testcode::

        import gymnasium as gym
        from ray.rllib.core.models.configs import MLPHeadConfig
        from ray.rllib.core.models.catalog import Catalog


        class MyCatalog(Catalog):
            def __init__(
                self,
                observation_space: gym.Space,
                action_space: gym.Space,
                model_config_dict: dict,
            ):
                super().__init__(observation_space, action_space, model_config_dict)
                self.my_model_config_dict = MLPHeadConfig(
                    hidden_layer_dims=[64, 32],
                    input_dim=self.observation_space.shape[0],
                    output_dim=1,
                )

            def build_my_head(self, framework: str):
                return self.my_model_config_dict.build(framework=framework)

        # With that, RLlib can build and use models from this catalog like this:
        catalog = MyCatalog(gym.spaces.Box(0, 1), gym.spaces.Box(0, 1), {})
        my_head = catalog.build_my_head("torch")  # doctest: +SKIP
        out = my_head(...)  # doctest: +SKIP
    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        # TODO (Artur): Turn model_config into model_config_dict to distinguish
        #  between ModelConfig and a model_config_dict dict library-wide.
        model_config_dict: dict,
        view_requirements: dict = None,
    ):
        """Initializes a Catalog with a default encoder config.

        Args:
            observation_space: The observation space of the environment.
            action_space: The action space of the environment.
            model_config_dict: The model config that specifies things like hidden
                dimensions and activations functions to use in this Catalog.
            view_requirements: The view requirements of Models to produce. This is
                needed for a Model that encodes a complex temporal mix of
                observations, actions or rewards.

        """
        self.observation_space = observation_space
        self.action_space = action_space

        # TODO (Artur): Make model defaults a dataclass
        self.model_config_dict = {**MODEL_DEFAULTS, **model_config_dict}
        self.view_requirements = view_requirements

        # Produce a basic encoder config.
        self.encoder_config = self.get_encoder_config(
            observation_space=observation_space,
            action_space=action_space,
            model_config_dict=model_config_dict,
            view_requirements=view_requirements,
        )

        # Get a mapping from framework to action distribution class
        self.action_dist_cls_dict = self.get_action_dist_cls_dict(
            action_space=action_space,
        )

        # The dimensions of the latent vector that is output by the encoder and fed
        # to the heads.
        self.latent_dim = self.encoder_config.output_dim

    def build_encoder(self, framework: str) -> Encoder:
        """Builds the encoder.

        By default this method builds an encoder instance from Catalog.encoder_config.

        Args:
            framework: The framework to use. Either "torch" or "tf".

        Returns:
            The encoder.
        """
        return self.encoder_config.build(framework=framework)

    @classmethod
    def get_encoder_config(
        cls,
        observation_space: gym.Space,
        model_config_dict: dict,
        action_space: gym.Space = None,
        view_requirements=None,
    ) -> ModelConfig:
        """Returns an EncoderConfig for the given input_space and model_config_dict.

        Encoders are usually used in RLModules to transform the input space into a
        latent space that is then fed to the heads. The returned EncoderConfig
        objects correspond to the built-in Encoder classes in RLlib.
        For example, for a simple 1D-Box input_space, RLlib offers an
        MLPEncoder, hence this method returns the MLPEncoderConfig. You can overwrite
        this method to produce specific EncoderConfigs for your custom Models.

        The following input spaces lead to the following configs:
        - 1D-Box: MLPEncoderConfig
        - 3D-Box: CNNEncoderConfig
        # TODO (Artur): Support more spaces here
        # ...

        Args:
            observation_space: The observation space to use.
            model_config_dict: The model config to use.
            action_space: The action space to use if actions are to be encoded. This
                is commonly the case for LSTM models.
            view_requirements: The view requirements to use if anything else than
                observation_space or action_space is to be encoded. This signifies an
                advanced use case.

        Returns:
            The encoder config.
        """
        # TODO (Artur): Make it so that we don't work with complete MODEL_DEFAULTS
        model_config_dict = {**MODEL_DEFAULTS, **model_config_dict}

        activation = model_config_dict["fcnet_activation"]
        output_activation = model_config_dict["fcnet_activation"]
        fcnet_hiddens = model_config_dict["fcnet_hiddens"]
        encoder_latent_dim = (
            model_config_dict["encoder_latent_dim"] or fcnet_hiddens[-1]
        )

        if model_config_dict["use_lstm"]:
            encoder_config = LSTMEncoderConfig(
                hidden_dim=model_config_dict["lstm_cell_size"],
                batch_first=not model_config_dict["_time_major"],
                num_layers=1,
                output_dim=model_config_dict["lstm_cell_size"],
                output_activation=output_activation,
                observation_space=observation_space,
                action_space=action_space,
                view_requirements_dict=view_requirements,
                get_tokenizer_config=cls.get_tokenizer_config,
            )
        elif model_config_dict["use_attention"]:
            raise NotImplementedError
        else:
            # TODO (Artur): Maybe check for original spaces here
            # input_space is a 1D Box
            if isinstance(observation_space, Box) and len(observation_space.shape) == 1:
                # In order to guarantee backward compatability with old configs,
                # we need to check if no latent dim was set and simply reuse the last
                # fcnet hidden dim for that purpose.
                if model_config_dict["encoder_latent_dim"]:
                    hidden_layer_dims = model_config_dict["fcnet_hiddens"]
                else:
                    hidden_layer_dims = model_config_dict["fcnet_hiddens"][:-1]
                encoder_config = MLPEncoderConfig(
                    input_dim=observation_space.shape[0],
                    hidden_layer_dims=hidden_layer_dims,
                    hidden_layer_activation=activation,
                    output_dim=encoder_latent_dim,
                    output_activation=output_activation,
                )

            # input_space is a 3D Box
            elif (
                isinstance(observation_space, Box) and len(observation_space.shape) == 3
            ):
                if not model_config_dict.get("conv_filters"):
                    model_config_dict["conv_filters"] = get_filter_config(
                        observation_space.shape
                    )

                encoder_config = CNNEncoderConfig(
                    input_dims=observation_space.shape,
                    filter_specifiers=model_config_dict["conv_filters"],
                    filter_layer_activation=activation,
                    output_activation=output_activation,
                    output_dim=encoder_latent_dim,
                )
            # input_space is a possibly nested structure of spaces.
            else:
                # NestedModelConfig
                raise NotImplementedError("No default config for complex spaces yet!")

        return encoder_config

    @classmethod
    def get_tokenizer_config(
        cls, space: gym.Space, model_config_dict: dict
    ) -> ModelConfig:
        """Returns a tokenizer config for the given space.

        This is useful for LSTM models that need to tokenize their inputs.
        By default, RLlib uses the models supported by Catalog out of the box to
        tokenize.
        """
        return cls.get_encoder_config(
            observation_space=space,
            # Use model_config_dict without flags that would end up in complex models
            model_config_dict={
                **model_config_dict,
                **{"use_lstm": False, "use_attention": False},
            },
        )

    @classmethod
    def get_action_dist_cls_dict(
        cls,
        action_space: gym.Space,
        deterministic: Optional[bool] = False,
    ) -> Mapping[str, Any]:
        """Returns a mapping from framework to distribution class.

        You can get the required input dimension for the distribution by calling
        `action_dict_cls.required_model_output_shape(action_space, model_config_dict)`
        on the retrieved class. This is useful, because the Catalog needs to find out
        about the required input dimension for the distribution before the model that
        outputs these inputs is configured.

        Args:
            action_space: Action space of the target gym env.
            deterministic: Whether to return a Deterministic distribution on input
                logits instead of a stochastic distributions. For example for Discrete
                spaces, the stochastic is a Categorical distribution with output logits,
                while the deterministic distribution will be to output the argmax of
                logits directly.

        Returns:
                Mapping from framework to distribution class.
        """
        distribution_dicts = {
            "deterministic": {"torch": TorchDeterministic, "tf": TfDeterministic},
            "gaussian": {"torch": TorchDiagGaussian, "tf": TfDiagGaussian},
            "categorical": {"torch": TorchCategorical, "tf": TfCategorical},
        }

        # Box space -> DiagGaussian OR Deterministic.
        if isinstance(action_space, Box):
            if action_space.dtype.char in np.typecodes["AllInteger"]:
                raise ValueError(
                    "Box(..., `int`) action spaces are not supported. "
                    "Use MultiDiscrete  or Box(..., `float`)."
                )
            else:
                if len(action_space.shape) > 1:
                    raise UnsupportedSpaceException(
                        "Action space has multiple dimensions "
                        "{}. ".format(action_space.shape)
                        + "Consider reshaping this into a single dimension, "
                        "using a custom action distribution, "
                        "using a Tuple action space, or the multi-agent API."
                    )
                if deterministic:
                    return distribution_dicts["deterministic"]
                else:
                    return distribution_dicts["gaussian"]

        # Discrete Space -> Categorical.
        elif isinstance(action_space, Discrete):
            return distribution_dicts["categorical"]

        # Tuple/Dict Spaces -> MultiAction.
        elif isinstance(action_space, (Tuple, Dict)):
            # TODO(Artur): Supported Tuple/Dict.
            raise NotImplementedError("Tuple/Dict spaces not yet supported.")

        # Simplex -> Dirichlet.
        elif isinstance(action_space, Simplex):
            # TODO(Artur): Supported Simplex (in torch).
            raise NotImplementedError("Simplex action space not yet supported.")

        # MultiDiscrete -> MultiCategorical.
        elif isinstance(action_space, MultiDiscrete):
            # TODO(Artur): Support multi-discrete.
            raise NotImplementedError("MultiDiscrete spaces not yet supported.")

        # Unknown type -> Error.
        else:
            raise NotImplementedError(f"Unsupported action space: `{action_space}`")

    def get_action_dist_cls(self, framework: str):
        """Get the action distribution class.

        The default behavior is to get the action distribution from the
        action_dist_cls_dict. This can be overridden to build a custom action
        distribution as a means of configuring the behavior of a PPORLModuleBase
        implementation.

        Args:
            framework: The framework to use. Either "torch" or "tf".

        Returns:
            The action distribution.
        """
        return self.action_dist_cls_dict[framework]
