import logging
from typing import Optional, Union

import gradio as gr
import numpy as np
import torch
import torch.profiler

from modules import refiner
from modules.api.utils import calc_spk_style
from modules.core.handler.datacls.audio_model import (
    AdjustConfig,
    AudioFormat,
    EncoderConfig,
)
from modules.core.handler.datacls.enhancer_model import EnhancerConfig
from modules.core.handler.datacls.tn_model import TNConfig
from modules.core.handler.datacls.tts_model import InferConfig, TTSConfig
from modules.core.handler.SSMLHandler import SSMLHandler
from modules.core.handler.TTSHandler import TTSHandler
from modules.core.handler.datacls.vc_model import VCConfig
from modules.core.models.tts import ChatTtsModel
from modules.core.spk import TTSSpeaker, spk_mgr
from modules.core.spk.TTSSpeaker import TTSSpeaker
from modules.core.ssml.SSMLParser import SSMLBreak, SSMLSegment, create_ssml_v01_parser
from modules.core.tn import ChatTtsTN
from modules.core.tools.SentenceSplitter import SentenceSplitter
from modules.data import styles_mgr
from modules.utils import audio_utils
from modules.utils.hf import spaces
from modules.webui import webui_config
from modules.webui.speaker.wav_misc import encode_to_wav
import time

logger = logging.getLogger(__name__)

SPK_FILE_EXTS = [
    # ".spkv1.json",
    # ".spkv1.png",
    ".json",
    ".png",
]


def get_speakers(filter: Optional[callable] = None) -> list[TTSSpeaker]:
    spks = spk_mgr.list_speakers()
    if filter is not None:
        spks = [spk for spk in spks if filter(spk)]

    return spks


def get_speaker_names(
    filter: Optional[callable] = None,
) -> tuple[list[TTSSpeaker], list[str]]:
    speakers = get_speakers(filter)

    def get_speaker_show_name(spk: TTSSpeaker):
        if spk.gender == "*" or spk.gender == "":
            return spk.name
        return f"{spk.gender} : {spk.name}"

    speaker_names = [get_speaker_show_name(speaker) for speaker in speakers]
    speaker_names.sort(key=lambda x: x.startswith("*") and "-1" or x)

    return speakers, speaker_names


def get_styles():
    return styles_mgr.list_items()


def load_spk_info(file):
    if file is None:
        return "empty"
    try:

        spk: TTSSpeaker = TTSSpeaker.from_file(file)
        return f"""
- name: {spk.name}
- gender: {spk.gender}
- describe: {spk.desc}
    """.strip()
    except Exception as e:
        logger.error(f"load spk info failed: {e}")
        return "load failed"


def segments_length_limit(
    segments: list[Union[SSMLBreak, SSMLSegment]], total_max: int
) -> list[Union[SSMLBreak, SSMLSegment]]:
    ret_segments = []
    total_len = 0
    for seg in segments:
        if isinstance(seg, SSMLBreak):
            ret_segments.append(seg)
            continue
        total_len += len(seg["text"])
        if total_len > total_max:
            break
        ret_segments.append(seg)
    return ret_segments


@torch.inference_mode()
@spaces.GPU(duration=120)
def synthesize_ssml(
    ssml: str,
    batch_size=4,
    enable_enhance=False,
    enable_denoise=False,
    eos: str = "[uv_break]",
    spliter_thr: int = 100,
    pitch: float = 0,
    speed_rate: float = 1,
    volume_gain_db: float = 0,
    normalize: bool = True,
    headroom: float = 1,
    progress=gr.Progress(track_tqdm=not webui_config.off_track_tqdm),
):
    try:
        batch_size = int(batch_size)
    except Exception:
        batch_size = 8

    ssml = ssml.strip()

    if ssml == "":
        raise gr.Error("SSML is empty, please input some SSML")

    parser = create_ssml_v01_parser()
    segments = parser.parse(ssml)
    max_len = webui_config.ssml_max
    segments = segments_length_limit(segments, max_len)

    if len(segments) == 0:
        raise gr.Error("No valid segments in SSML")

    infer_config = InferConfig(
        batch_size=batch_size,
        spliter_threshold=spliter_thr,
        eos=eos,
        # NOTE: SSML not support `infer_seed` contorl
        # seed=42,
        # NOTE: 开启以支持 track_tqdm
        sync_gen=True,
    )
    adjust_config = AdjustConfig(
        pitch=pitch,
        speed_rate=speed_rate,
        volume_gain_db=volume_gain_db,
        normalize=normalize,
        headroom=headroom,
    )
    enhancer_config = EnhancerConfig(
        enabled=enable_denoise or enable_enhance or False,
        lambd=0.9 if enable_denoise else 0.1,
    )
    encoder_config = EncoderConfig(
        format=AudioFormat.mp3,
        bitrate="64k",
    )
    tts_config = TTSConfig(mid="chat-tts")

    handler = SSMLHandler(
        ssml_content=ssml,
        tts_config=tts_config,
        infer_config=infer_config,
        adjust_config=adjust_config,
        enhancer_config=enhancer_config,
        encoder_config=encoder_config,
    )

    sample_rate, audio_data = handler.enqueue()

    # NOTE: 这里必须要加，不然 gradio 没法解析成 mp3 格式
    audio_data = audio_utils.audio_to_int16(audio_data)

    return sample_rate, audio_data


# @torch.inference_mode()
@spaces.GPU(duration=120)
def tts_generate(
    text,
    temperature=0.3,
    top_p=0.7,
    top_k=20,
    spk=-1,
    infer_seed=-1,
    use_decoder=True,
    prompt1="",
    prompt2="",
    prefix="",
    style="",
    disable_normalize=False,
    batch_size=4,
    enable_enhance=False,
    enable_denoise=False,
    spk_file=None,
    spliter_thr: int = 100,
    eos: str = "[uv_break]",
    pitch: float = 0,
    speed_rate: float = 1,
    volume_gain_db: float = 0,
    normalize: bool = True,
    headroom: float = 1,
    ref_audio: Optional[tuple[int, np.ndarray]] = None,
    ref_audio_text: Optional[str] = None,
    model_id: str = "chat-tts",
    progress=gr.Progress(track_tqdm=not webui_config.off_track_tqdm),
):
    try:
        batch_size = int(batch_size)
    except Exception:
        batch_size = 4

    max_len = webui_config.tts_max
    text = text.strip()[0:max_len]

    if text == "":
        raise gr.Error("Text is empty, please input some text")

    if style == "*auto":
        style = ""

    if isinstance(top_k, float):
        top_k = int(top_k)

    params = calc_spk_style(spk=spk, style=style)
    spk = params.get("spk", spk)

    infer_seed = infer_seed or params.get("seed", infer_seed)
    temperature = temperature or params.get("temperature", temperature)
    prefix = prefix or params.get("prefix", "")
    prompt = params.get("prompt", "")
    prompt1 = prompt1 or params.get("prompt1", "")
    prompt2 = prompt2 or params.get("prompt2", "")

    infer_seed = np.clip(infer_seed, -1, 2**32 - 1, out=None, dtype=np.float64)
    infer_seed = int(infer_seed)

    # ref: https://github.com/2noise/ChatTTS/issues/123#issue-2326908144
    min_n = 0.000000001
    if temperature == 0.1:
        temperature = min_n

    if isinstance(spk, int):
        spk = ChatTtsModel.ChatTTSModel.create_speaker_from_seed(spk)

    if spk_file:
        try:
            spk: TTSSpeaker = TTSSpeaker.from_file(spk_file)
        except Exception:
            raise gr.Error("Failed to load speaker file")

    if ref_audio is not None:
        if ref_audio_text is None or ref_audio_text.strip() == "":
            raise gr.Error("ref_audio_text is empty")
        ref_audio_bytes = encode_to_wav(audio_tuple=ref_audio)
        spk = TTSSpeaker.from_ref_wav_bytes(
            ref_wav=ref_audio_bytes,
            text=ref_audio_text,
        )

    tts_config = TTSConfig(
        mid=model_id,
        style=style,
        temperature=temperature,
        top_k=top_k,
        top_p=top_p,
        prompt=prompt,
        prefix=prefix,
        prompt1=prompt1,
        prompt2=prompt2,
    )
    infer_config = InferConfig(
        batch_size=batch_size,
        spliter_threshold=spliter_thr,
        eos=eos,
        seed=infer_seed,
        # NOTE: 开启以支持 track_tqdm
        sync_gen=True,
    )
    adjust_config = AdjustConfig(
        pitch=pitch,
        speed_rate=speed_rate,
        volume_gain_db=volume_gain_db,
        normalize=normalize,
        headroom=headroom,
    )
    enhancer_config = EnhancerConfig(
        enabled=enable_denoise or enable_enhance or False,
        lambd=0.9 if enable_denoise else 0.1,
    )
    encoder_config = EncoderConfig(
        format=AudioFormat.mp3,
        bitrate="64k",
    )

    print(f"Start Generate Audio\n TTSConfig: {tts_config}\n InferConfig: {infer_config}\n AdjustConfig: {adjust_config}\n EnhancerConfig: {enhancer_config}\n EncoderConfig: {encoder_config}\n Text:{text}\n")
    start_time = time.perf_counter()

    handler = TTSHandler(
        text_content=text,
        spk=spk,
        tts_config=tts_config,
        infer_config=infer_config,
        adjust_config=adjust_config,
        enhancer_config=enhancer_config,
        encoder_config=encoder_config,
        vc_config=VCConfig(enabled=False),
    )

    sample_rate, audio_data = handler.enqueue()

    end_time = time.perf_counter()
    print(f"TTS Elapsed Time: {round(end_time - start_time, 2)}s")

    # NOTE: 这里必须要加，不然 gradio 没法解析成 mp3 格式
    audio_data = audio_utils.audio_to_int16(audio_data)
    return sample_rate, audio_data


@torch.inference_mode()
def text_normalize(text: str) -> str:
    return ChatTtsTN.ChatTtsTN.normalize(
        text, config=TNConfig(disabled=["replace_unk_tokens"])
    )


@torch.inference_mode()
@spaces.GPU(duration=120)
def refine_text(
    text: str,
    oral: int = -1,
    speed: int = -1,
    rf_break: int = -1,
    laugh: int = -1,
    # TODO 这个还没ui
    spliter_threshold: int = 300,
    progress=gr.Progress(track_tqdm=not webui_config.off_track_tqdm),
):
    text = text_normalize(text)
    prompt = []
    if oral != -1:
        prompt.append(f"[oral_{oral}]")
    if speed != -1:
        prompt.append(f"[speed_{speed}]")
    if rf_break != -1:
        prompt.append(f"[break_{rf_break}]")
    if laugh != -1:
        prompt.append(f"[laugh_{laugh}]")
    return refiner.refine_text(
        text, prompt="".join(prompt), spliter_threshold=spliter_threshold
    )


@torch.inference_mode()
@spaces.GPU(duration=120)
def split_long_text(long_text_input, spliter_threshold=100, eos=""):
    # TODO 传入 tokenizer
    spliter = SentenceSplitter(threshold=spliter_threshold)
    sentences = spliter.parse(long_text_input)
    sentences = [text_normalize(s) + eos for s in sentences]
    data = []
    for i, text in enumerate(sentences):
        token_length = spliter.len(text)
        data.append([i, text, token_length])
    return data
