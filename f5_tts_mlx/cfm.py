"""
ein notation:
b - batch
n - sequence
nt - text sequence
nw - raw wave length
d - dimension
"""

from __future__ import annotations
from datetime import datetime
from pathlib import Path
from random import random
from typing import Callable, Literal

import mlx.core as mx
import mlx.nn as nn

from einops.array_api import rearrange, repeat

from vocos_mlx import Vocos

from f5_tts_mlx.duration import DurationPredictor, DurationTransformer
from f5_tts_mlx.dit import DiT
from f5_tts_mlx.modules import MelSpec
from f5_tts_mlx.utils import (
    exists,
    default,
    lens_to_mask,
    mask_from_frac_lengths,
    list_str_to_idx,
    list_str_to_tensor,
    pad_sequence,
    fetch_from_hub,
)

# conditional flow matching


class F5TTS(nn.Module):
    def __init__(
        self,
        transformer: nn.Module,
        sigma=0.0,
        audio_drop_prob=0.3,
        cond_drop_prob=0.2,
        num_channels=None,
        mel_spec_module: nn.Module | None = None,
        mel_spec_kwargs: dict = dict(),
        frac_lengths_mask: tuple[float, float] = (0.7, 1.0),
        vocab_char_map: dict[str, int] | None = None,
        vocoder: Callable[[mx.array["b d n"]], mx.array["b nw"]] | None = None,
        duration_predictor: DurationPredictor | None = None,
    ):
        super().__init__()

        self.frac_lengths_mask = frac_lengths_mask

        # mel spec
        self._mel_spec = default(mel_spec_module, MelSpec(**mel_spec_kwargs))
        num_channels = default(num_channels, self._mel_spec.n_mels)
        self.num_channels = num_channels

        # classifier-free guidance
        self.audio_drop_prob = audio_drop_prob
        self.cond_drop_prob = cond_drop_prob

        # transformer
        self.transformer = transformer
        dim = transformer.dim
        self.dim = dim

        # conditional flow related
        self.sigma = sigma

        # vocab map for tokenization
        self._vocab_char_map = vocab_char_map

        # vocoder (optional)
        self._vocoder = vocoder

        # duration predictor (optional)
        self._duration_predictor = duration_predictor

    def __call__(
        self,
        inp: mx.array["b n d"] | mx.array["b nw"],  # mel or raw wave
        text: mx.array["b nt"] | list[str],
        *,
        lens: mx.array["b"] | None = None,
    ) -> tuple[mx.array, mx.array, mx.array]:
        # handle raw wave
        if inp.ndim == 2:
            inp = self._mel_spec(inp)
            inp = rearrange(inp, "b d n -> b n d")
            assert inp.shape[-1] == self.num_channels

        batch, seq_len, dtype, σ1 = *inp.shape[:2], inp.dtype, self.sigma

        # handle text as string
        if isinstance(text, list):
            if exists(self._vocab_char_map):
                text = list_str_to_idx(text, self._vocab_char_map)
            else:
                text = list_str_to_tensor(text)
            assert text.shape[0] == batch

        # lens and mask
        if not exists(lens):
            lens = mx.full((batch,), seq_len)

        mask = lens_to_mask(lens, length=seq_len)

        # get a random span to mask out for training conditionally
        frac_lengths = mx.random.uniform(*self.frac_lengths_mask, (batch,))
        rand_span_mask = mask_from_frac_lengths(lens, frac_lengths, max_length=seq_len)

        if exists(mask):
            rand_span_mask = rand_span_mask & mask

        # mel is x1
        x1 = inp

        # x0 is gaussian noise
        x0 = mx.random.normal(x1.shape)

        # time step
        time = mx.random.uniform(0, 1, (batch,), dtype=dtype)

        # sample xt (φ_t(x) in the paper)
        t = rearrange(time, "b -> b 1 1")
        φ = (1 - t) * x0 + t * x1
        flow = x1 - x0

        # only predict what is within the random mask span for infilling
        cond = mx.where(
            rand_span_mask[..., None],
            mx.zeros_like(x1),
            x1,
        )

        # transformer and cfg training with a drop rate
        drop_audio_cond = random() < self.audio_drop_prob  # p_drop in voicebox paper
        if random() < self.cond_drop_prob:
            drop_audio_cond = True
            drop_text = True
        else:
            drop_text = False

        # if want rigourously mask out padding, record in collate_fn in dataset.py, and pass in here
        # adding mask will use more memory, thus also need to adjust batchsampler with scaled down threshold for long sequences
        pred = self.transformer(
            x=φ,
            cond=cond,
            text=text,
            time=time,
            drop_audio_cond=drop_audio_cond,
            drop_text=drop_text,
        )

        # flow matching loss
        loss = nn.losses.mse_loss(pred, flow, reduction="none")

        rand_span_mask = repeat(rand_span_mask, "b n -> b n d", d=self.num_channels)
        masked_loss = mx.where(rand_span_mask, loss, mx.zeros_like(loss))
        loss = mx.sum(masked_loss) / mx.maximum(mx.sum(rand_span_mask), 1e-6)

        return loss.mean()

    def odeint_midpoint(self, func, y0, t):
        """
        Solves ODE using the midpoint method.

        Parameters:
        - y0: Initial state, an MLX array of any shape.
        - t: Array of time steps, an MLX array.
        """
        ys = [y0]
        y_current = y0

        for i in range(len(t) - 1):
            t_current = t[i]
            dt = t[i + 1] - t_current

            # midpoint approximation
            k1 = func(t_current, y_current)
            mid = y_current + 0.5 * dt * k1

            # compute the next value
            k2 = func(t_current + 0.5 * dt, mid)
            y_next = y_current + dt * k2

            ys.append(y_next)
            y_current = y_next

        return mx.stack(ys)

    def odeint_euler(self, func, y0, t):
        """
        Solves ODE using the Euler method.

        Parameters:
        - y0: Initial state, an MLX array of any shape.
        - t: Array of time steps, an MLX array.
        """
        ys = [y0]
        y_current = y0

        for i in range(len(t) - 1):
            t_current = t[i]
            dt = t[i + 1] - t_current

            # compute the next value
            k = func(t_current, y_current)
            y_next = y_current + dt * k

            ys.append(y_next)
            y_current = y_next

        return mx.stack(ys)

    def sample(
        self,
        cond: mx.array["b n d"] | mx.array["b nw"],
        text: mx.array["b nt"] | list[str],
        duration: int | mx.array["b"] | None = None,
        *,
        lens: mx.array["b"] | None = None,
        steps=32,
        method: Literal["euler", "midpoint"] = "euler",
        cfg_strength=2.0,
        speed=1.0,
        sway_sampling_coef=-1.0,
        seed: int | None = None,
        max_duration=4096,
        no_ref_audio=False,
        edit_mask=None,
    ) -> tuple[mx.array, mx.array]:
        start_date = datetime.now()

        self.eval()

        # raw wave

        if cond.ndim == 2:
            cond = rearrange(cond, "1 n -> n")
            cond = self._mel_spec(cond)
            # cond = rearrange(cond, "b d n -> b n d")
            assert cond.shape[-1] == self.num_channels

        batch, cond_seq_len, dtype = *cond.shape[:2], cond.dtype
        if not exists(lens):
            lens = mx.full((batch,), cond_seq_len, dtype=dtype)

        # text

        if isinstance(text, list):
            if exists(self._vocab_char_map):
                text = list_str_to_idx(text, self._vocab_char_map)
            else:
                text = list_str_to_tensor(text)
            assert text.shape[0] == batch

        if exists(text):
            text_lens = (text != -1).sum(axis=-1)
            lens = mx.maximum(text_lens, lens)

        # duration

        if duration is None and self._duration_predictor is not None:
            duration_in_sec = self._duration_predictor(cond, text)
            frame_rate = self._mel_spec.sample_rate // self._mel_spec.hop_length
            duration = (duration_in_sec * frame_rate / speed).astype(mx.int32).item()
            print(
                f"Got duration of {duration} frames ({duration_in_sec.item()} secs) for generated speech."
            )
        elif duration is None:
            raise ValueError(
                "Duration must be provided or a duration predictor must be set."
            )

        cond_mask = lens_to_mask(lens)
        if edit_mask is not None:
            cond_mask = cond_mask & edit_mask

        if isinstance(duration, int):
            duration = mx.full((batch,), duration, dtype=dtype)

        duration = mx.maximum(lens + 1, duration)
        duration = mx.clip(duration, 0, max_duration)
        max_duration = int(duration.max().item())

        cond = mx.pad(cond, [(0, 0), (0, max_duration - cond_seq_len), (0, 0)])
        cond_mask = mx.pad(
            cond_mask,
            [(0, 0), (0, max_duration - cond_mask.shape[-1])],
            constant_values=False,
        )
        cond_mask = rearrange(cond_mask, "... -> ... 1")

        # at each step, conditioning is fixed

        step_cond = mx.where(cond_mask, cond, mx.zeros_like(cond))

        if batch > 1:
            mask = lens_to_mask(duration)
        else:
            mask = None

        # test for no ref audio
        if no_ref_audio:
            cond = mx.zeros_like(cond)

        # neural ode

        def fn(t, x):
            # predict flow
            pred = self.transformer(
                x=x,
                cond=step_cond,
                text=text,
                time=t,
                mask=mask,
                drop_audio_cond=False,
                drop_text=False,
            )
            if cfg_strength < 1e-5:
                mx.eval(pred)
                return pred

            null_pred = self.transformer(
                x=x,
                cond=step_cond,
                text=text,
                time=t,
                mask=mask,
                drop_audio_cond=True,
                drop_text=True,
            )
            output = pred + (pred - null_pred) * cfg_strength
            mx.eval(output)
            return output

        # noise input
        
        y0 = []
        for dur in duration:
            if exists(seed):
                mx.random.seed(seed)
            y0.append(mx.random.normal((dur, self.num_channels)))
        y0 = pad_sequence(y0, padding_value=0)

        t_start = 0

        t = mx.linspace(t_start, 1, steps)
        if exists(sway_sampling_coef):
            t = t + sway_sampling_coef * (mx.cos(mx.pi / 2 * t) - 1 + t)

        if method == "midpoint":
            trajectory = self.odeint_midpoint(fn, y0, t)
        elif method == "euler":
            trajectory = self.odeint_euler(fn, y0, t)
        else:
            raise ValueError(f"Unknown method: {method}")

        sampled = trajectory[-1]
        out = sampled
        out = mx.where(cond_mask, cond, out)

        if exists(self._vocoder):
            out = self._vocoder(out)

        mx.eval(out)

        print(f"Generated speech in {datetime.now() - start_date}")

        return out, trajectory

    @classmethod
    def from_pretrained(cls, hf_model_name_or_path: str, bit = None) -> F5TTS:
        if bit is None:
            if "8bit" in hf_model_name_or_path:
                print("Loading model with 8bit quantization")
                bit = 8
            elif "4bit" in hf_model_name_or_path:
                print("Loading model with 8bit quantization")
                bit = 4

        path = fetch_from_hub(hf_model_name_or_path)

        if path is None:
            raise ValueError(f"Could not find model {hf_model_name_or_path}")

        # vocab

        vocab_path = path / "vocab.txt"
        vocab = {v: i for i, v in enumerate(Path(vocab_path).read_text().split("\n"))}
        if len(vocab) == 0:
            raise ValueError(f"Could not load vocab from {vocab_path}")

        # duration predictor

        duration_model_path = path / "duration_v2.safetensors"
        duration_predictor = None

        if duration_model_path.exists():
            duration_predictor = DurationPredictor(
                transformer=DurationTransformer(
                    dim=512,
                    depth=8,
                    heads=8,
                    text_dim=512,
                    ff_mult=2,
                    conv_layers=2,
                    text_num_embeds=len(vocab) - 1,
                ),
                vocab_char_map=vocab,
            )
            weights = mx.load(duration_model_path.as_posix(), format="safetensors")
            duration_predictor.load_weights(list(weights.items()))

        # vocoder

        vocos = Vocos.from_pretrained("lucasnewman/vocos-mel-24khz")

        # model

        model_path = path / "model.safetensors"

        f5tts = F5TTS(
            transformer=DiT(
                dim=1024,
                depth=22,
                heads=16,
                ff_mult=2,
                text_dim=512,
                conv_layers=4,
                text_num_embeds=len(vocab) - 1,
            ),
            vocab_char_map=vocab,
            vocoder=vocos.decode,
            duration_predictor=duration_predictor,
        )

        if bit is not None:
            nn.quantize(f5tts, bits = bit, class_predicate= lambda p, m: isinstance(m, nn.Linear) and m.weight.shape[1] % 64 == 0)

        weights = mx.load(model_path.as_posix(), format="safetensors")
        f5tts.load_weights(list(weights.items()))
        mx.eval(f5tts.parameters())

        return f5tts
