from tqdm import tqdm
from PIL import Image

import torch
from torch._tensor import Tensor
import torch.nn as nn

from transformers import AutoTokenizer
from safetensors.torch import load_file
from accelerate import init_empty_weights

from .config import AuraFlowConig
from .denoiser import (
    Denoiser,
    DENOISER_TENSOR_PREFIX,
)
from .text_encoder import (
    TextEncoder,
    DEFAULT_TEXT_ENCODER_CONFIG,
    DEFAULT_TEXT_ENCODER_CLASS,
    DEFAULT_TEXT_ENCODER_CONFIG_CLASS,
    DEFAULT_TOKENIZER_REPO,
    DEFAULT_TOKENIZER_FOLDER,
    TEXT_ENCODER_TENSOR_PREFIX,
)
from .vae import VAE, DEFAULT_VAE_CONFIG
from .scheduler import Scheduler
from ...utils import tensor as tensor_utils


class AuraFlowModel(nn.Module):
    def __init__(self, config: AuraFlowConig):
        super().__init__()

        self.config = config

        self.denoiser = Denoiser.from_config(config.denoiser_config)
        vae = VAE.from_config(DEFAULT_VAE_CONFIG)
        assert isinstance(vae, VAE)
        self.vae = vae
        _text_encoder = DEFAULT_TEXT_ENCODER_CLASS._from_config(
            DEFAULT_TEXT_ENCODER_CONFIG_CLASS(**DEFAULT_TEXT_ENCODER_CONFIG),
        )
        _tokenizer = AutoTokenizer.from_pretrained(
            DEFAULT_TOKENIZER_REPO, subfolder=DEFAULT_TOKENIZER_FOLDER
        )
        self.text_encoder = TextEncoder(model=_text_encoder, tokenizer=_tokenizer)

        self.scheduler = Scheduler()
        self.progress_bar = tqdm

    @classmethod
    def from_config(cls, config: AuraFlowConig) -> "AuraFlowModel":
        return cls(config)

    def _load_original_weights(
        self,
        torch_dtype: torch.dtype = torch.bfloat16,
    ):
        config = self.config
        state_dict = load_file(config.checkpoint_path)
        self.denoiser.load_state_dict(
            {
                key[len(DENOISER_TENSOR_PREFIX) :]: value.to(torch_dtype)
                for key, value in state_dict.items()
                if key.startswith(DENOISER_TENSOR_PREFIX)
            },
            assign=True,
        )
        self.vae = VAE.from_pretrained(
            config.pretrained_model_name_or_path,
            subfolder=config.vae_folder,
            torch_dtype=torch_dtype,
        )
        self.text_encoder.model.load_state_dict(
            {
                key[len(TEXT_ENCODER_TENSOR_PREFIX) :]: value.to(torch_dtype)
                for key, value in state_dict.items()
                if key.startswith(TEXT_ENCODER_TENSOR_PREFIX)
            },
            assign=True,
        )

    @classmethod
    def from_original_checkpoint(
        cls,
        config: AuraFlowConig,
        torch_dtype: torch.dtype = torch.bfloat16,
    ) -> "AuraFlowModel":
        with init_empty_weights():
            model = cls.from_config(config)

        model._load_original_weights(torch_dtype)

        return model

    def prepare_latents(
        self,
        batch_size: int,
        height: int,
        width: int,
        dtype: torch.dtype,
        device: torch.device,
        seed: int | None = None,
        latents: torch.Tensor | None = None,
    ) -> torch.Tensor:
        latent_channels = self.denoiser.config.in_channels

        if latents is None:
            shape = (
                batch_size,
                latent_channels,
                int(height) // self.vae.compression_ratio,
                int(width) // self.vae.compression_ratio,
            )
            latents = tensor_utils.incremental_seed_randn(
                shape,
                seed=seed,
                dtype=dtype,
                device=device,
            )
        else:
            latents = latents.to(dtype=dtype, device=device)

        return latents

    @torch.no_grad()
    def encode_image(
        self,
        image: Image.Image | list[Image.Image],
    ) -> torch.Tensor:
        _images = image if isinstance(image, list) else [image]

        _images = tensor_utils.images_to_tensor(
            _images, self.vae.dtype, self.vae.device
        )
        encode_output = self.vae.encode(_images)
        latents = encode_output[0].sample() * self.vae.scaling_factor

        return latents

    @torch.no_grad()
    def decode_image(
        self,
        latents: torch.Tensor,
    ) -> list[Image.Image]:
        image = self.vae.decode(
            latents / self.vae.scaling_factor,  # type: ignore
            return_dict=False,
        )[0]
        image = tensor_utils.tensor_to_images(image)

        return image

    def generate(
        self,
        prompt: str | list[str],
        negative_prompt: str | list[str] | None = None,
        width: int = 768,
        height: int = 768,
        num_inference_steps: int = 20,
        cfg_scale: float = 1.0,
        seed: int | None = None,
        max_token_length: int = 256,
        device: torch.device | str = torch.device("cuda"),
        do_offloading: bool = False,
    ) -> list[Image.Image]:
        # 1. Prepare args
        execution_device = device
        denoiser_dtype = next(self.denoiser.parameters()).dtype
        do_cfg = cfg_scale > 1.0
        timesteps, num_inference_steps = self.scheduler.retrieve_timesteps(
            num_inference_steps,
            execution_device,
            sigmas=None,
        )
        batch_size = len(prompt) if isinstance(prompt, list) else 1

        # 2. Encode text
        if do_offloading:
            self.text_encoder.to(device)
        encoder_output = self.text_encoder.encode_prompts(
            prompt,
            negative_prompt,
            use_negative_prompts=do_cfg,
            max_token_length=max_token_length,
        )
        if do_offloading:
            self.text_encoder.to("cpu")

        # 3. Prepare latents.
        latents = self.prepare_latents(
            batch_size,
            height,
            width,
            denoiser_dtype,
            execution_device,
            seed=seed,
        )

        # 4. Denoising loop
        if do_offloading:
            self.denoiser.to(device)
        num_warmup_steps = max(
            len(timesteps) - num_inference_steps * self.scheduler.order, 0
        )
        latents = latents.to(self.denoiser.device)
        prompt_embeddings = torch.cat(
            [
                encoder_output.positive_embeddings,
                encoder_output.negative_embeddings,
            ]
        ).to(self.denoiser.device)
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            # current_timestep is 1000.0 -> 0
            for i, current_timestep in enumerate(timesteps):
                # expand the latents if we are doing classifier free guidance
                latent_model_input = torch.cat([latents] * 2) if do_cfg else latents

                # aura use timestep value between 0 and 1, with t=1 as noise and t=0 as the image
                # broadcast to batch dimension in a way that's compatible with ONNX/Core ML
                batch_timestep = torch.tensor([current_timestep / 1000]).expand(
                    latent_model_input.shape[0]
                )
                batch_timestep = batch_timestep.to(latents.device, dtype=latents.dtype)

                # predict noise model_output
                noise_pred = self.denoiser(
                    latent=latent_model_input,
                    encoder_hidden_states=prompt_embeddings,
                    timestep=batch_timestep,
                )

                # perform cfg
                if do_cfg:
                    noise_pred_positive, noise_pred_negative = noise_pred.chunk(2)
                    noise_pred = noise_pred_negative + cfg_scale * (
                        noise_pred_positive - noise_pred_negative
                    )

                # compute the previous noisy sample x_t -> x_t-1
                latents = self.scheduler.step(
                    noise_pred,
                    current_timestep,
                    latents,
                    return_dict=False,
                )[0]

                # call the callback, if provided
                if i == len(timesteps) - 1 or (
                    (i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0
                ):
                    progress_bar.update()
        if do_offloading:
            self.denoiser.to("cpu")

        # 5. Decode the latents
        if do_offloading:
            self.vae.to(device)
        image = self.decode_image(latents.to(self.vae.device))
        if do_offloading:
            self.vae.to("cpu")

        return image