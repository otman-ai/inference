from __future__ import annotations

import os
import re
import subprocess
import tarfile
from time import perf_counter
from typing import TYPE_CHECKING, Any, Dict, Tuple, Type

from inference.core.cache.model_artifacts import (
    get_cache_dir,
    get_cache_file_path,
    save_bytes_in_cache,
)
from inference.core.entities.responses.inference import (
    InferenceResponseImage,
    LMMInferenceResponse,
)
from inference.core.env import DEVICE, HUGGINGFACE_TOKEN, MODEL_CACHE_DIR
from inference.core.exceptions import ModelArtefactError
from inference.core.logger import logger
from inference.core.models.base import PreprocessReturnMetadata
from inference.core.models.roboflow import RoboflowInferenceModel
from inference.core.roboflow_api import (
    ModelEndpointType,
    get_from_url,
    get_roboflow_base_lora,
    get_roboflow_instant_model_data,
    get_roboflow_model_data,
)
from inference.core.utils.image_utils import load_image_rgb

if TYPE_CHECKING:
    import torch
    from PIL import Image
    from transformers import AutoModel, AutoProcessor

    from inference.core.entities.responses.inference import InferenceResponseImage


class TransformerModel(RoboflowInferenceModel):
    task_type = "lmm"
    transformers_class: Type[AutoModel] | None = None
    processor_class: Type[AutoProcessor] | None = None
    default_dtype: Type[torch.dtype] | None = None
    generation_includes_input = False
    needs_hf_token = False
    skip_special_tokens = True
    load_weights_as_transformers = False
    load_base_from_roboflow = True
    model = None

    def __init__(
        self, model_id, *args, dtype=None, huggingface_token=HUGGINGFACE_TOKEN, **kwargs
    ):
        if TransformerModel.transformers_class is None:
            from transformers import AutoModel, AutoProcessor

            TransformerModel.transformers_class = AutoModel
            TransformerModel.processor_class = AutoProcessor

        import torch

        if TransformerModel.default_dtype is None:
            TransformerModel.default_dtype = torch.float16

        global DEVICE
        if DEVICE is None:
            DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"

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

        self.initialize_model()

    def initialize_model(self):
        if not self.load_base_from_roboflow:
            model_id = self.dataset_id
        else:
            model_id = self.cache_dir

        self.model = (
            self.transformers_class.from_pretrained(
                model_id,
                cache_dir=self.cache_dir,
                device_map=DEVICE,
                token=self.huggingface_token,
                torch_dtype=self.default_dtype,
            )
            .eval()
            .to(self.dtype)
        )

        self.processor = self.processor_class.from_pretrained(
            model_id, cache_dir=self.cache_dir, token=self.huggingface_token
        )

    def preprocess(
        self, image: Any, **kwargs
    ) -> Tuple[Image.Image, PreprocessReturnMetadata]:
        from PIL import Image

        pil_image = Image.fromarray(load_image_rgb(image))
        image_dims = pil_image.size

        return pil_image, PreprocessReturnMetadata({"image_dims": image_dims})

    def postprocess(
        self,
        predictions: Tuple[str],
        preprocess_return_metadata: PreprocessReturnMetadata,
        **kwargs,
    ) -> LMMInferenceResponse:
        text = predictions[0]
        image_dims = preprocess_return_metadata["image_dims"]
        response = LMMInferenceResponse(
            response=text,
            image=InferenceResponseImage(width=image_dims[0], height=image_dims[1]),
        )
        return [response]

    def predict(self, image_in: Image.Image, prompt="", history=None, **kwargs):
        model_inputs = self.processor(
            text=prompt, images=image_in, return_tensors="pt"
        ).to(self.model.device)
        input_len = model_inputs["input_ids"].shape[-1]

        import torch

        with torch.inference_mode():
            prepared_inputs = self.prepare_generation_params(
                preprocessed_inputs=model_inputs
            )
            generation = self.model.generate(
                **prepared_inputs,
                max_new_tokens=1000,
                do_sample=False,
                early_stopping=False,
                no_repeat_ngram_size=0,
            )
            generation = generation[0]
            if self.generation_includes_input:
                generation = generation[input_len:]

            decoded = self.processor.decode(
                generation, skip_special_tokens=self.skip_special_tokens
            )

        return (decoded,)

    def prepare_generation_params(
        self, preprocessed_inputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        return preprocessed_inputs

    def get_infer_bucket_file_list(self) -> list:
        """Get the list of required files for inference.

        Returns:
            list: A list of required files for inference, e.g., ["model.pt"].
        """
        return [
            "config.json",
            "special_tokens_map.json",
            "generation_config.json",
            "tokenizer.json",
            re.compile(r"model.*\.safetensors"),
            "preprocessor_config.json",
            "tokenizer_config.json",
        ]

    def download_model_artifacts_from_roboflow_api(self) -> None:
        if self.load_weights_as_transformers:
            api_data = get_roboflow_model_data(
                api_key=self.api_key,
                model_id=self.endpoint,
                endpoint_type=ModelEndpointType.CORE_MODEL,
                device_id=self.device_id,
            )
            if "weights" not in api_data:
                raise ModelArtefactError(
                    f"`weights` key not available in Roboflow API response while downloading model weights."
                )
            weights = api_data["weights"]
        elif self.version_id is not None:
            api_data = get_roboflow_model_data(
                api_key=self.api_key,
                model_id=self.endpoint,
                endpoint_type=ModelEndpointType.ORT,
                device_id=self.device_id,
            )
            if "weights" not in api_data["ort"]:
                raise ModelArtefactError(
                    f"`weights` key not available in Roboflow API response while downloading model weights."
                )
            weights = api_data["ort"]["weights"]
        else:
            api_data = get_roboflow_instant_model_data(
                api_key=self.api_key,
                model_id=self.endpoint,
            )
            if "modelFiles" not in api_data:
                raise ModelArtefactError(
                    f"`modelFiles` key not available in Roboflow API response while downloading model weights."
                )
            if "transformers" not in api_data["modelFiles"]:
                raise ModelArtefactError(
                    f"`transformers` key not available in Roboflow API response while downloading model weights."
                )
            weights = api_data["modelFiles"]["transformers"]
        files_to_download = list(weights.keys())
        for file_name in files_to_download:
            weights_url = weights[file_name]
            t1 = perf_counter()
            filename = weights_url.split("?")[0].split("/")[-1]
            if filename.endswith(".npz"):
                continue
            model_weights_response = get_from_url(weights_url, json_response=False)
            save_bytes_in_cache(
                content=model_weights_response.content,
                file=filename,
                model_id=self.endpoint,
            )
            if filename.endswith("tar.gz"):
                try:
                    subprocess.run(
                        [
                            "tar",
                            "-xzf",
                            os.path.join(self.cache_dir, filename),
                            "-C",
                            self.cache_dir,
                        ],
                        check=True,
                    )
                except subprocess.CalledProcessError as e:
                    raise ModelArtefactError(
                        f"Failed to extract model archive {filename}. Error: {str(e)}"
                    ) from e

            if perf_counter() - t1 > 120:
                logger.debug(
                    "Weights download took longer than 120 seconds, refreshing API request"
                )
                if self.load_weights_as_transformers:
                    api_data = get_roboflow_model_data(
                        api_key=self.api_key,
                        model_id=self.endpoint,
                        endpoint_type=ModelEndpointType.CORE_MODEL,
                        device_id=self.device_id,
                    )
                    weights = api_data["weights"]
                elif self.version_id is not None:
                    api_data = get_roboflow_model_data(
                        api_key=self.api_key,
                        model_id=self.endpoint,
                        endpoint_type=ModelEndpointType.ORT,
                        device_id=self.device_id,
                    )
                    weights = api_data["ort"]["weights"]
                else:
                    api_data = get_roboflow_instant_model_data(
                        api_key=self.api_key,
                        model_id=self.endpoint,
                    )
                    weights = api_data["modelFiles"]["transformers"]

    @property
    def weights_file(self) -> None:
        return None

    def download_model_artefacts_from_s3(self) -> None:
        raise NotImplementedError()


class LoRATransformerModel(TransformerModel):
    load_base_from_roboflow = False

    def initialize_model(self):
        import torch
        from peft import LoraConfig
        from peft.peft_model import PeftModel

        lora_config = LoraConfig.from_pretrained(self.cache_dir, device_map=DEVICE)
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
        self.base_model = self.transformers_class.from_pretrained(
            model_load_id,
            revision=revision,
            device_map=DEVICE,
            cache_dir=cache_dir,
            token=token,
        ).to(self.dtype)
        self.model = (
            PeftModel.from_pretrained(self.base_model, self.cache_dir)
            .eval()
            .to(self.dtype)
        )

        self.model.merge_and_unload()

        self.processor = self.processor_class.from_pretrained(
            model_load_id, revision=revision, cache_dir=cache_dir, token=token
        )

    def get_lora_base_from_roboflow(self, repo, revision) -> str:
        base_dir = os.path.join("lora-bases", repo, revision)
        cache_dir = get_cache_dir(base_dir)
        if os.path.exists(cache_dir):
            return cache_dir
        api_data = get_roboflow_base_lora(self.api_key, repo, revision, self.device_id)
        if "weights" not in api_data:
            raise ModelArtefactError(
                f"`weights` key not available in Roboflow API response while downloading model weights."
            )

        weights_url = api_data["weights"]["model"]
        model_weights_response = get_from_url(weights_url, json_response=False)
        filename = weights_url.split("?")[0].split("/")[-1]
        assert filename.endswith("tar.gz")
        save_bytes_in_cache(
            content=model_weights_response.content,
            file=filename,
            model_id=base_dir,
        )
        tar_file_path = get_cache_file_path(filename, base_dir)
        with tarfile.open(tar_file_path, "r:gz") as tar:
            tar.extractall(path=cache_dir)

        return cache_dir

    def get_infer_bucket_file_list(self) -> list:
        """Get the list of required files for inference.

        Returns:
            list: A list of required files for inference, e.g., ["model.pt"].
        """
        return [
            "adapter_config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "adapter_model.safetensors",
            "preprocessor_config.json",
            "tokenizer_config.json",
        ]
