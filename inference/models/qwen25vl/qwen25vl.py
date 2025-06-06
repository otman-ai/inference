import json
import os

import torch
from peft import LoraConfig, PeftModel
from PIL import Image
from transformers import (
    AutoModelForCausalLM,
    BitsAndBytesConfig,
    Qwen2_5_VLConfig,
    Qwen2_5_VLForConditionalGeneration,
)

from inference.core.env import DEVICE, HUGGINGFACE_TOKEN, MODEL_CACHE_DIR
from inference.models.transformers import LoRATransformerModel, TransformerModel

AutoModelForCausalLM.register(
    config_class=Qwen2_5_VLConfig, model_class=Qwen2_5_VLForConditionalGeneration
)

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_quant_storage=torch.bfloat16,
)


def _patch_preprocessor_config(cache_dir: str):
    """
    Checks and patches the preprocessor_config.json in the given cache directory
    to ensure the image_processor_type is recognized.
    """
    config_path = os.path.join(cache_dir, "preprocessor_config.json")
    target_key = "image_processor_type"
    correct_value = "Qwen2VLImageProcessor"

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Preprocessor config not found at {config_path}")

    with open(config_path, "r") as f:
        data = json.load(f)

    if target_key in data and data[target_key] != correct_value:
        data[target_key] = correct_value
        with open(config_path, "w") as f:
            json.dump(data, f, indent=4)
    elif target_key in data:
        pass
    else:
        raise ValueError(f"'{target_key}' not found in {config_path}")


class Qwen25VL(TransformerModel):
    generation_includes_input = True
    transformers_class = AutoModelForCausalLM
    default_dtype = torch.bfloat16
    skip_special_tokens = False

    default_system_prompt = (
        "You are a Qwen2.5-VL model that can answer questions about any image."
    )

    def __init__(
        self,
        model_id,
        *args,
        dtype=None,
        huggingface_token=HUGGINGFACE_TOKEN,
        use_quantization=True,
        **kwargs,
    ):
        super().__init__(model_id, *args, **kwargs)
        self.huggingface_token = huggingface_token
        if self.needs_hf_token and self.huggingface_token is None:
            raise RuntimeError(
                "Must set environment variable HUGGINGFACE_TOKEN to load LoRA "
                "(or pass huggingface_token to this __init__)"
            )
        self.dtype = dtype
        if self.dtype is None:
            self.dtype = self.default_dtype
        self.cache_model_artefacts()

        self.cache_dir = os.path.join(MODEL_CACHE_DIR, self.endpoint + "/")
        self.use_quantization = use_quantization
        self.initialize_model()

    def initialize_model(self):
        config_file = os.path.join(self.cache_dir, "adapter_config.json")

        with open(config_file, "r") as file:
            config = json.load(file)

        keys_to_remove = ["eva_config", "lora_bias", "exclude_modules"]

        for key in keys_to_remove:
            config.pop(key, None)

        with open(config_file, "w") as file:
            json.dump(config, file, indent=2)

        lora_config = LoraConfig(**config)
        model_id = lora_config.base_model_name_or_path
        revision = lora_config.revision
        if revision is not None:
            try:
                self.dtype = getattr(torch, revision)
            except AttributeError:
                pass
        if not self.load_base_from_roboflow:
            model_load_id = model_id
            cache_dir = os.path.join(MODEL_CACHE_DIR, "huggingface")
            revision = revision
            token = self.huggingface_token
        else:
            model_load_id = self.get_lora_base_from_roboflow(model_id, revision)
            cache_dir = model_load_id
            revision = None
            token = None

        files_folder = MODEL_CACHE_DIR + "lora-bases/qwen/qwen25vl-7b/main/"
        _patch_preprocessor_config(files_folder)

        if self.use_quantization:
            self.base_model = self.transformers_class.from_pretrained(
                model_load_id,
                revision=revision,
                device_map=DEVICE,
                cache_dir=cache_dir,
                token=token,
                quantization_config=bnb_config,
            )
        else:
            self.base_model = self.transformers_class.from_pretrained(
                model_load_id,
                revision=revision,
                device_map=DEVICE,
                cache_dir=cache_dir,
                token=token,
            )
        self.model = self.base_model.eval().to(self.dtype)

        preprocessor_config_path = os.path.join(self.cache_dir, "chat_template.json")
        with open(preprocessor_config_path, "r") as f:
            chat_template = json.load(f)["chat_template"]

        self.processor = self.processor_class.from_pretrained(
            model_load_id,
            revision=revision,
            cache_dir=cache_dir,
            token=token,
            chat_template=chat_template,
        )

    def predict(self, image_in: Image.Image, prompt=None, **kwargs):
        split_prompt = prompt.split("<system_prompt>")
        if len(split_prompt) == 1:
            prompt = split_prompt[0]
            system_prompt = self.default_system_prompt
        else:
            prompt = split_prompt[0]
            system_prompt = split_prompt[1]

        conversation = [
            {
                "role": "system",
                "content": [{"type": "text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image_in},
                    {"type": "text", "text": prompt or ""},
                ],
            },
        ]

        text_input = self.processor.apply_chat_template(conversation, tokenize=False)

        model_inputs = self.processor(
            text=text_input,
            images=image_in,
            return_tensors="pt",
            padding=True,
        )

        model_inputs = {
            k: v.to(self.model.device)
            for k, v in model_inputs.items()
            if isinstance(v, torch.Tensor)
        }

        input_len = model_inputs["input_ids"].shape[-1]

        with torch.inference_mode():
            generation = self.model.generate(
                **model_inputs,
                max_new_tokens=512,
                pad_token_id=self.processor.tokenizer.pad_token_id,
                eos_token_id=self.processor.tokenizer.eos_token_id,
                bos_token_id=self.processor.tokenizer.bos_token_id,
            )

        if not self.generation_includes_input:
            generation = generation[:, input_len:]

        decoded = self.processor.decode(
            generation[0],
            skip_special_tokens=self.skip_special_tokens,
        )

        decoded = decoded.replace("assistant\n", "")
        decoded = decoded.replace(" addCriterion\n", "")
        return (decoded,)


class LoRAQwen25VL(LoRATransformerModel):
    load_base_from_roboflow = True
    generation_includes_input = True
    skip_special_tokens = True
    transformers_class = AutoModelForCausalLM
    default_dtype = torch.bfloat16
    use_quantization = True

    default_system_prompt = (
        "You are a Qwen2.5-VL model that can answer questions about any image."
    )

    def get_lora_base_from_roboflow(self, model_id, revision):
        cache_dir = super().get_lora_base_from_roboflow(model_id, revision)
        return cache_dir

    def initialize_model(self):
        config_file = os.path.join(self.cache_dir, "adapter_config.json")

        with open(config_file, "r") as file:
            config = json.load(file)

        keys_to_remove = ["eva_config", "lora_bias", "exclude_modules"]

        for key in keys_to_remove:
            config.pop(key, None)

        with open(config_file, "w") as file:
            json.dump(config, file, indent=2)

        lora_config = LoraConfig(**config)
        model_id = lora_config.base_model_name_or_path
        revision = lora_config.revision
        if revision is not None:
            try:
                self.dtype = getattr(torch, revision)
            except AttributeError:
                pass
        if not self.load_base_from_roboflow:
            model_load_id = model_id
            cache_dir = os.path.join(MODEL_CACHE_DIR, "huggingface")
            revision = revision
            token = self.huggingface_token
        else:
            model_load_id = self.get_lora_base_from_roboflow(model_id, revision)
            cache_dir = model_load_id
            revision = None
            token = None

        rm_weights = os.path.join(
            MODEL_CACHE_DIR, "lora-bases/qwen/qwen25vl-7b/main/weights.tar.gz"
        )
        if os.path.exists(rm_weights):
            os.remove(rm_weights)

        files_folder = MODEL_CACHE_DIR + "lora-bases/qwen/qwen25vl-7b/main/"
        _patch_preprocessor_config(files_folder)

        if self.use_quantization:
            self.base_model = self.transformers_class.from_pretrained(
                model_load_id,
                revision=revision,
                device_map=DEVICE,
                cache_dir=cache_dir,
                token=token,
                quantization_config=bnb_config,
            )
        else:
            self.base_model = self.transformers_class.from_pretrained(
                model_load_id,
                revision=revision,
                device_map=DEVICE,
                cache_dir=cache_dir,
                token=token,
            )

        if model_load_id != "qwen-pretrains/1":
            self.model = (
                PeftModel.from_pretrained(self.base_model, self.cache_dir)
                .eval()
                .to(self.dtype)
            )
        else:
            self.model = self.base_model.eval().to(self.dtype)

        self.model.merge_and_unload()
        preprocessor_config_path = os.path.join(self.cache_dir, "chat_template.json")
        with open(preprocessor_config_path, "r") as f:
            chat_template = json.load(f)["chat_template"]

        self.processor = self.processor_class.from_pretrained(
            model_load_id,
            revision=revision,
            cache_dir=cache_dir,
            token=token,
            chat_template=chat_template,
            min_pixels=256 * 28 * 28,
            max_pixels=1280 * 28 * 28,
        )

    def predict(self, image_in: Image.Image, prompt=None, **kwargs):
        if prompt is None:
            prompt = ""
            system_prompt = self.default_system_prompt
        else:
            split_prompt = prompt.split("<system_prompt>")
            if len(split_prompt) == 1:
                prompt = split_prompt[0]
                system_prompt = self.default_system_prompt
            else:
                prompt = split_prompt[0]
                system_prompt = split_prompt[1]

        conversation = [
            {
                "role": "system",
                "content": [{"type": "text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image_in},
                    {"type": "text", "text": prompt},
                ],
            },
        ]

        text_input = self.processor.apply_chat_template(conversation, tokenize=False)

        model_inputs = self.processor(
            text=text_input,
            images=image_in,
            return_tensors="pt",
            padding=True,
        )

        model_inputs = {
            k: v.to(self.model.device)
            for k, v in model_inputs.items()
            if isinstance(v, torch.Tensor)
        }

        input_len = model_inputs["input_ids"].shape[-1]

        with torch.inference_mode():
            generation = self.model.generate(
                **model_inputs,
                max_new_tokens=512,
                pad_token_id=self.processor.tokenizer.pad_token_id,
                eos_token_id=self.processor.tokenizer.eos_token_id,
                bos_token_id=self.processor.tokenizer.bos_token_id,
            )

        if self.generation_includes_input:
            generation = generation[:, input_len:]

        decoded = self.processor.decode(
            generation[0],
            skip_special_tokens=self.skip_special_tokens,
        )

        decoded = decoded.replace("assistant\n", "")
        decoded = decoded.replace(" addCriterion\n", "")

        return (decoded,)
