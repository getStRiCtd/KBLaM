import argparse
import json
import logging
import os
import pathlib
import re
from functools import partial
from itertools import chain
from typing import Callable, Dict, List, Optional

import numpy as np
import torch
import transformers
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeRemainingColumn
from rich.theme import Theme
from torch.nn import CrossEntropyLoss
from torch.optim.optimizer import ParamsT
from transformers import AutoTokenizer

from kblam.kb_encoder import KBEncoder
from kblam.models.llama_model import KblamForCausalLM
from kblam.models.phi3_model import KbphiForCausalLM
from kblam.utils.data_utils import aug_row, generate_multi_entity_qa, get_i_dont_know_ans
from kblam.utils.train_utils import get_kb_embd

LOGFORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOGFORMAT_RICH = "%(message)s"

# setup logging
# Create a custom theme for Rich
custom_theme = Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "bold red",
        "critical": "bold white on red",
    }
)

# Create a Rich console with the custom theme
console = Console(theme=custom_theme)

# Configure the root logger to WARNING
logging.basicConfig(
    level=logging.WARNING,  # Set the root logger to WARNING
    format=LOGFORMAT_RICH,
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)

# fmt: off
parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, default=1)
parser.add_argument("--train_dataset",type=str,default="synthetic_data",choices=["enron", "synthetic_data"])
parser.add_argument("--N", type=int, default=1000, help="Size of training set, select the first N samples for training")
parser.add_argument("--B", type=int, default=10, help="Batch size")
parser.add_argument("--lr", type=float, default=1e-3, help="Learning rate")
parser.add_argument("--load_epoch", type=int, default=None, help="load from checkpoint")
parser.add_argument("--freeze_v_proj", action="store_true", help="Freeze the value projector head")
parser.add_argument("--tune_llm_q", action=argparse.BooleanOptionalAction, help="Fine tune the query head")
parser.add_argument("--sep_query_head", action=argparse.BooleanOptionalAction, help="Fine tune the query head")
parser.add_argument("--use_oai_embd", action="store_true", help="Use OpenAI embedding")
parser.add_argument("--use_cached_embd", action="store_true", help="Choose to use pre-computed KV embeddings")
parser.add_argument("--epoch", type=int, default=300, help="Total epochs")
parser.add_argument("--encoder_spec", type=str, default="OAI")
parser.add_argument("--key_embd_src", type=str, default="key", choices=["key", "answer", "questions", None], help="Source of key embedding")
parser.add_argument("--use_data_aug", action="store_true", help="Randomly pick templates for the question")
parser.add_argument("--use_lr_decay", action="store_true")
parser.add_argument("--dataset_dir", type=str, default="synthetic_data",help="Where is the data saved?")
parser.add_argument("--model_dir", type=str, default=None, help="Where is the base model saved")
parser.add_argument("--hf_model_spec", type=str, default="microsoft/Phi-3-mini-4k-instruct", choices=["meta-llama/Meta-Llama-3-8B", "microsoft/Phi-3-mini-4k-instruct"])
parser.add_argument("--hf_token", type=str,default=None,help="Huggingface token")
parser.add_argument("--model_save_dir", type=str, default="output", help="Place to save the checkpoints")
parser.add_argument("--log_save_dir", type=str, help="Place to save the training log")
parser.add_argument("--kb_size", type=int, default=None, help="Place to save the training log")
parser.add_argument("--duplicate_true_kb", action=argparse.BooleanOptionalAction, default=True, help="Duplicate true entity's KB token")
parser.add_argument("--length_invariance", action=argparse.BooleanOptionalAction, default=False, help="Scale the raw attention score")
parser.add_argument("--outlier_ratio", type=int, default=-1, help="Introduce questions without correct KB entites")
parser.add_argument("--multi_entities", type=int, default=None, help="Introduce questions involving multiple entities")
parser.add_argument("--use_extended_qa", action="store_true", help="Introduce QA with extended open-ended parts")
parser.add_argument("--kb_token_layer_frequency", type=int, default=None, help="Introduce QA with extended open-ended parts")
parser.add_argument("--gradient_accm_step", type=int, default=20, help="Introduce QA with extended open-ended parts")
parser.add_argument("--use_cuda", action="store_true", help="Use cuda if available")
parser.add_argument("--verbose", action="store_true", help="Set logging to debug")
parser.add_argument("--log_to_file", action="store_true", help="Log to file as well as stdout")
parser.add_argument("--llm_type",type=str,default="phi3",choices=["llama3", "phi3"])
# fmt: on


def create_custom_progress_bar(
    console: Console = None,  # type: ignore
    color: str = "cyan",
    show_time: bool = True,
    show_spinner: bool = True,
    spinner_style: str = "dots",
) -> Progress:
    """
    Create a custom progress bar using Rich, optionally including loss reporting.

    :param description: Description of the task
    :param total: Total number of steps
    :param console: Rich Console object (if None, a new one will be created)
    :param color: Color of the progress bar
    :param show_time: Whether to show the time remaining
    :param show_spinner: Whether to show a spinner
    :param spinner_style: Style of the spinner (e.g., "dots", "dots12", "line", "arrow")
    :param show_loss: Whether to show loss information
    :return: A Rich Progress object and task ID
    """
    if console is None:
        console = Console()
    columns = []

    if show_spinner:
        columns.append(SpinnerColumn(spinner_name=spinner_style, style=color))

    columns.extend(
        [
            TextColumn("[bold blue]{task.description}", justify="right"),
            BarColumn(bar_width=None, style=color, complete_style=f"bold {color}"),
            TaskProgressColumn(),
            TextColumn("[bold yellow]Loss: {task.fields[loss]:.4f}", justify="right"),
        ]
    )

    if show_time:
        columns.append(TimeRemainingColumn())

    progress = Progress(*columns, console=console, expand=True, disable=False)
    return progress


def _format_QA_llama(Q: str, A: str):
    return (
        "<|start_header_id|>user<|end_header_id|> "
        + Q
        + "<|eot_id|>"
        + "<|start_header_id|>assistant<|end_header_id|>"
        + A
        + "<|eot_id|>"
    )


def _format_QA_phi3(Q: str, A: str):
    return "<|user|>\n" + Q + "<|end|>\n" + "<|assistant|>\n" + A + "<|end|>\n"


def _create_labels_for_llama(input_ids: torch.Tensor, input_strs: List[str], tokenizer):
    # Not sure this is correct. This method simply masks the <|start_header_id|>user<|end_header_id|> then leaves the rest in the labels
    # Possibly what they want is to mask out the query. To do that swap the index from the tokenizer below from 1 to 2
    answer_indices = torch.argmax(
        (input_ids == tokenizer("<|start_header_id|>assistant<|end_header_id|>")["input_ids"][1]).long(),
        -1,
    )
    answer_mask = torch.ones_like(input_ids)
    for b in range(len(input_strs)):
        answer_mask[b, : (answer_indices[b].item() + 2)] = 0
    labels = input_ids * answer_mask + (1 - answer_mask) * (-100)
    return labels


def _create_labels_for_phi3(input_ids: torch.Tensor, input_strs: List[str], tokenizer):
    # We just want to mask out the starting token.
    # The tokenized values are left padded so we want to know where our Q/A pairs start
    # Not 100% this is correct
    answer_indices = torch.argmax(
        (input_ids == tokenizer("<|user|>")["input_ids"][0]).long(),
        -1,
    )
    answer_mask = torch.ones_like(input_ids)
    for b in range(len(input_strs)):
        answer_mask[b, : (answer_indices[b].item() + 1)] = 0
    labels = input_ids * answer_mask + (1 - answer_mask) * (-100)
    return labels


def get_batch(
    qa_format_func: Callable[[str, str], str],
    label_func: Callable[[torch.Tensor, List, Callable], torch.Tensor],
    dataset: List[Dict],
    tokenizer,
    device: torch.device,
    B: int = 20,
    random_sample=True,
    use_data_aug=False,
    include_outlier=False,
    multi_entities=None,
    use_extended_qa=False,
):
    """
    dataset: List of dictionary, denoting the KB, used to extract QA pairs
    model: The LLM, used to provide the embedding
    kb_embedding: KB embedding (differentiable)
    B: Batchsize
    include_outlier : Create a batch of question without answer in the KB.
    multi_entities : Create a batch of question that involves more than one entities.
    """
    labels = []
    if multi_entities is not None:
        assert not include_outlier
    if random_sample:
        if multi_entities is not None:
            batch_indices = np.random.choice(len(dataset), (B, multi_entities), replace=False)
        else:
            batch_indices = np.random.choice(len(dataset), B, replace=False)
    else:
        batch_indices = np.arange(B)

    def get_question_and_answer(idx):
        if use_extended_qa:
            Q, A = dataset[idx]["extended_Q"], dataset[idx]["extended_A"]
        elif multi_entities is not None:
            Q, A = generate_multi_entity_qa(
                [dataset[i]["name"] for i in idx],
                [dataset[i]["description_type"] for i in idx],
                [dataset[i]["description"] for i in idx],
            )
        else:
            Q = aug_row(dataset[idx]) if use_data_aug else dataset[idx]["Q"]
            # Maybe we can replace this with gpt_augment(answer_str)
            A = get_i_dont_know_ans() if include_outlier else dataset[idx]["A"]
        return Q, A

    with torch.autograd.no_grad():
        input_strs = []
        for idx in batch_indices:
            Q, A = get_question_and_answer(idx)
            input_strs.append(qa_format_func(Q, A))
        tokenizer_output = tokenizer(input_strs, return_tensors="pt", padding=True).to(device)
        input_ids, attention_masks = (
            tokenizer_output["input_ids"],
            tokenizer_output["attention_mask"],
        )

        labels = label_func(input_ids, input_strs, tokenizer)
    if include_outlier:
        # Generate a new set of indices, such that the KB does not contain the entity where the question comes from
        batch_indices = np.random.choice(len(dataset), B, replace=False)
    return input_ids, attention_masks, labels, batch_indices


def get_prefix_str(args):
    use_data_aug = args.use_data_aug
    sep_query_head = args.sep_query_head
    kb_size = args.kb_size
    if kb_size == -1:
        kb_size = None  # Progressively increase size
    elif kb_size == 0:
        kb_size = "dynamic"  # Random size
    duplicate_true_kb = args.duplicate_true_kb
    length_invariance = args.length_invariance
    outlier_ratio = args.outlier_ratio
    use_outlier = outlier_ratio != -1
    multi_entities = args.multi_entities
    use_extended_qa = args.use_extended_qa
    kb_token_layer_frequency = args.kb_token_layer_frequency
    lr = args.lr

    prefix_string = f"stage1_lr_{lr}"
    if kb_token_layer_frequency is not None:
        prefix_string += f"KBTokenLayerFreq{kb_token_layer_frequency}"
    if use_extended_qa:
        prefix_string += "UseExtendedQA"
    if multi_entities is not None:
        prefix_string += f"MultiEntities{multi_entities}"
    if use_outlier:
        prefix_string += f"UseOutlier{outlier_ratio}"
    if length_invariance:
        prefix_string += "LengthInvariant"
    if not duplicate_true_kb:
        prefix_string += "NoDuplicate"
    if kb_size is not None:
        prefix_string += f"KBSize{kb_size}"
    if sep_query_head:
        prefix_string += "SepQueryHead"
    if use_data_aug:
        prefix_string += "UseDataAug"
    return prefix_string


def _load_cached_embeddigns(encoder_model_spec: str, dataset_dir: str, dataset_name: str, key_embd_src: str):

    if encoder_model_spec == "OAI":
        encoder_model_spec_str = "oai"
    else:
        encoder_model_spec_str = encoder_model_spec
    key_embds = np.load(
        os.path.join(
            dataset_dir,
            f"{dataset_name}_{encoder_model_spec_str}_embd_{key_embd_src}.npy",
        )
    ).astype("float32")
    if key_embd_src == "answer":
        # If we are using the answer string as the key, we also use it as the value string
        value_embds = np.load(
            os.path.join(
                dataset_dir,
                f"{dataset_name}_{encoder_model_spec_str}_embd_answer.npy",
            )
        ).astype("float32")
    else:
        value_embds = np.load(
            os.path.join(
                dataset_dir,
                f"{dataset_name}_{encoder_model_spec_str}_embd_value.npy",
            )
        ).astype("float32")
    return key_embds, value_embds


def context_set_size_scheduler(epoch: int, kb_size: str | int):
    if kb_size is not None:
        if kb_size == "dynamic":
            return np.random.randint(4, 100)
        return kb_size
    round = (epoch) // 100
    return 4 * (round + 1)


def get_step_config(
    current_accum_step: int,
    total_accum_step: int,
    use_data_aug: bool,
    outlier_ratio: int,
    multi_entities: int | None,
    use_extended_qa: bool,
):
    """
    Our instruction tuning dataset is composed of different types of instructions.
    Strategies:
    Outlier QA takes the last `outlier_ratio` accum steps;
    Multiple entites QA (if included) takes 1/3 of the rest accum_steps;
    Extended QA (if included) takes 1/3 of the rest accum_steps;
    Standard QA takes the rest.
    """
    use_outlier = outlier_ratio != -1
    config = {}
    config["use_data_aug"] = use_data_aug
    config["include_outlier"] = False
    config["multi_entities"] = None
    config["use_extended_qa"] = False
    if use_outlier:
        include_outlier = current_accum_step >= total_accum_step - 1 - outlier_ratio
        # Decide to include outlier and has reached the time
        if include_outlier:
            config["include_outlier"] = True
            return config
    if current_accum_step % 3 == 0:
        # multi_entities could be None,
        # in which case we just use standard QA
        config["multi_entities"] = multi_entities
        return config
    if current_accum_step % 3 == 1:
        config["use_extended_qa"] = use_extended_qa
        return config
    return config


def _get_parameter_count(encoder):
    param_count = 0.0
    for p in encoder.parameters():
        if p.requires_grad:
            param_count += p.numel()
    return param_count


def _get_phi3_query_head_parameters(
    model: KblamForCausalLM | KbphiForCausalLM,
    sep_query_head: bool,
    actual_kb_token_layer_frequency: int,
):
    llm_q_params = []
    for name, param in model.named_parameters():
        if sep_query_head:
            # For phi3
            if "qkv_proj.weight" in name:
                layer_id = int(re.search(r"\d+", name)[0])  # type: ignore
                if layer_id % actual_kb_token_layer_frequency == 0:
                    old_weight = param.detach()
            if "q_proj_new.weight" in name:
                layer_id = int(re.search(r"\d+", name)[0])  # type: ignore
                if layer_id % actual_kb_token_layer_frequency == 0:
                    param.copy_(old_weight[: model.config.hidden_size, :])  # type: ignore
                    param.requires_grad = True
                    llm_q_params.append(param)
        else:
            if "q_proj.weight" in name:
                layer_id = int(re.search(r"\d+", name)[0])  # type: ignore
                if layer_id % actual_kb_token_layer_frequency == 0:
                    param.requires_grad = True
                    llm_q_params.append(param)
    return llm_q_params


def _get_llama3_query_head_parameters(
    model: KblamForCausalLM | KbphiForCausalLM,
    sep_query_head: bool,
    actual_kb_token_layer_frequency: int,
):
    llm_q_params = []
    for name, param in model.named_parameters():
        if sep_query_head:  # TODO: this is different for each model type
            # For llama3
            if "q_proj.weight" in name:
                layer_id = int(re.search(r"\d+", name)[0])  # type: ignore
                if layer_id % actual_kb_token_layer_frequency == 0:
                    old_weight = param.detach()
            if "q_proj_new.weight" in name:
                layer_id = int(re.search(r"\d+", name)[0])  # type: ignore
                if layer_id % actual_kb_token_layer_frequency == 0:
                    param.copy_(old_weight)  # type: ignore
                    param.requires_grad = True
                    llm_q_params.append(param)
        else:
            if "q_proj.weight" in name:
                layer_id = int(re.search(r"\d+", name)[0])  # type: ignore
                if layer_id % actual_kb_token_layer_frequency == 0:
                    param.requires_grad = True
                    llm_q_params.append(param)
    return llm_q_params


class KBRetriever:
    def __init__(
        self,
        encoder: KBEncoder,
        dataset: List[Dict],
        key_embds: Optional[np.ndarray],
        value_embds: Optional[np.ndarray],
    ):
        self.encoder = encoder
        self.key_embds = key_embds
        self.value_embds = value_embds
        self.dataset = dataset

    def _use_cached_embd(self):
        if self.key_embds is not None and self.value_embds is not None:
            return True
        else:
            return False

    def get_key_embeddings(self, batch_indices, batch_size, epoch, kb_size):
        if self._use_cached_embd():
            train_set_key, train_set_val = get_kb_embd(
                self.encoder,
                batch_indices,
                precomputed_embd=(self.key_embds, self.value_embds),
            )
        else:
            train_set_key, train_set_val = get_kb_embd(self.encoder, batch_indices, kb_dict=self.dataset)

        if len(train_set_key.shape) == 2:
            # Add comment on why we need this line
            train_set_key = train_set_key.unsqueeze(0).transpose(0, 1)
            train_set_val = train_set_val.unsqueeze(0).transpose(0, 1)

        context_set_size = context_set_size_scheduler(epoch, kb_size)
        context_set_index = np.random.choice(len(self.dataset), context_set_size, replace=False)  # type: ignore
        if self._use_cached_embd():
            context_set_key, context_set_val = get_kb_embd(
                self.encoder,
                context_set_index,
                precomputed_embd=(self.key_embds, self.value_embds),
            )
        else:
            context_set_key, context_set_val = get_kb_embd(self.encoder, context_set_index, kb_dict=self.dataset)
        context_set_key = context_set_key.unsqueeze(0).expand(batch_size, *context_set_key.shape)
        context_set_val = context_set_val.unsqueeze(0).expand(batch_size, *context_set_val.shape)
        # context_set_val = torch.randn_like(context_set_val)
        # Idea: Try torch.randn here context_set_tokens??
        true_kb_copy = 1
        kb_embedding = (
            torch.concat([*([train_set_key] * true_kb_copy), context_set_key], 1),
            torch.concat([*([train_set_val] * true_kb_copy), context_set_val], 1),
        )
        return kb_embedding


def _setup_scheduler_and_optimizer(model_parapmeters: ParamsT, lr: float, max_iter: int):
    optim = torch.optim.AdamW(model_parapmeters, lr=lr, weight_decay=0.0)  # type: ignore

    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optim, max_iter, eta_min=lr * 0.01)  # type: ignore
    return scheduler, optim


class Trainer:
    def __init__(
        self,
        llm_model: KbphiForCausalLM | KblamForCausalLM,
        kbretriever: KBRetriever,
        tokenizer: transformers.PreTrainedTokenizer,
        actual_kb_token_layer_frequency: int,
        num_epochs: int,
        lr: float,
        device: torch.device,
        use_lr_decay: bool,
        kb_size: int,
        llm_savename: str,
        encoder_savename: str,
        output_dir: str,
        fine_tune_llm_q: bool = False,
        sep_query_head: bool = False,
    ):
        self.logger = logging.getLogger("training")
        self.tokenizer = tokenizer
        self.sep_query_head = sep_query_head
        self.actual_kb_token_layer_frequency = actual_kb_token_layer_frequency
        self.num_epochs = num_epochs
        self.lr = lr
        self.model = llm_model
        self.fine_tune_llm_q = fine_tune_llm_q
        self.device = device
        self.kbretriever = kbretriever
        self.kb_size = kb_size
        self.use_lr_decay = use_lr_decay
        self.llm_savename = llm_savename
        self.encoder_savename = encoder_savename
        self.output_path = pathlib.Path(output_dir)

        if isinstance(llm_model, KbphiForCausalLM):  # Phi3
            self._get_batch = partial(get_batch, _format_QA_phi3, _create_labels_for_phi3)
            self._get_params = _get_phi3_query_head_parameters
        elif isinstance(llm_model, KblamForCausalLM):  # llama
            self._get_batch = partial(get_batch, _format_QA_llama, _create_labels_for_llama)
            self._get_params = _get_llama3_query_head_parameters
        else:
            raise ValueError(f"{llm_model} not recognised")

        self.scheduler, self.optim = self.setup_scheduler_and_optim()

    def setup_scheduler_and_optim(self):
        if self.fine_tune_llm_q:
            self.logger.info("Query head being fine tuned!")
            llm_q_params = self._get_params(self.model, self.sep_query_head, self.actual_kb_token_layer_frequency)
            scheduler, optim = _setup_scheduler_and_optimizer(
                chain(self.kbretriever.encoder.parameters(), llm_q_params),
                self.lr,
                self.num_epochs,
            )
            self.logger.info("Optimizer recreated")
        else:
            scheduler, optim = _setup_scheduler_and_optimizer(
                self.kbretriever.encoder.parameters(), self.lr, self.num_epochs
            )
            self.logger.info("Optimizer recreated")
        return scheduler, optim

    def train(
        self,
        training_set: List[Dict],
        batch_size,
        grad_accum_steps: int,
        outlier_ratio: int,
        use_data_aug: bool = False,
        multi_entities: bool = False,
        use_extended_qa: bool = False,
        save_period: int = 2000,
        fine_tune_llm_q: Optional[bool] = False,
    ):
        train_losses = []
        start_epoch = 0
        with create_custom_progress_bar(console=console) as pbar:
            task = pbar.add_task("Training", total=self.num_epochs, loss=100)
            for epoch in range(start_epoch, self.num_epochs, 1):

                self.optim.zero_grad()
                losses = []

                # Accumulate gradients
                for a_step in range(grad_accum_steps):
                    step_config = get_step_config(
                        a_step,
                        grad_accum_steps,
                        use_data_aug,
                        outlier_ratio,
                        multi_entities,
                        use_extended_qa,
                    )
                    input_ids, attention_masks, labels, batch_indices = self._get_batch(
                        training_set,
                        self.tokenizer,
                        self.device,
                        B=batch_size,
                        random_sample=True,
                        **step_config,
                    )
                    kb_embedding = self.kbretriever.get_key_embeddings(batch_indices, batch_size, epoch, self.kb_size)
                    out = self.model(
                        input_ids=input_ids,
                        attention_mask=attention_masks,
                        kb_kv=kb_embedding,
                        output_attentions=True,
                        kb_config={
                            "sep_query_head": self.sep_query_head,
                            "eval_mode": False,
                            "kb_layer_frequency": self.actual_kb_token_layer_frequency,
                        },
                    )
                    logits = out["logits"]

                    # display ground truth and model prediction to quickly check model
                    if a_step == 0 and epoch % 10 == 0:
                        batch_index = 0  # Which example in the batch to select
                        max_logits = logits.argmax(axis=2)
                        decoded_pred = self.tokenizer.decode(max_logits[batch_index, :-1])
                        sel_labels = labels[batch_index, :]
                        sel_labels = sel_labels[sel_labels >= 0]  # Remove padding token -100
                        decoded_gt = self.tokenizer.decode(sel_labels)
                        self.logger.info(f"{decoded_gt}")
                        self.logger.info(f"{decoded_pred}")

                    shift_logits = logits[..., :-1, :].contiguous()
                    shift_labels = labels[..., 1:].contiguous()
                    weights = (shift_labels > 0).sum(-1, keepdim=True).expand(-1, shift_labels.shape[1]).contiguous()
                    # Flatten the tokens
                    shift_logits = shift_logits.view(-1, self.model.config.vocab_size)
                    shift_labels = shift_labels.view(-1)
                    weights = weights.view(-1)
                    loss_fct = CrossEntropyLoss(reduction="none")
                    shift_labels = shift_labels.to(shift_logits.device)

                    loss = (
                        loss_fct(shift_logits, shift_labels) * weights.max() / weights
                    ).mean()  # Make sure each sample is equally weighted

                    loss.backward()
                    losses.append(loss.item())

                self.optim.step()
                if self.use_lr_decay:
                    self.scheduler.step()

                self.logger.info(f"Epoch: {epoch}, loss: {np.mean(losses)}")

                train_losses.append(np.mean(losses))
                pbar.update(task, advance=1, loss=np.mean(losses))
                if epoch % save_period == 0:
                    ckpt_name = self.output_path / f"{self.encoder_savename}_epoch_{epoch}"
                    torch.save(self.kbretriever.encoder.state_dict(), ckpt_name)

                if fine_tune_llm_q:
                    if (epoch % save_period) == 0 and (epoch != start_epoch):
                        model_ckpt_name = self.output_path / f"{self.llm_savename}_epoch_{epoch}"
                        self.model.save_pretrained(model_ckpt_name)


def main():
    logger = logging.getLogger("training")

    args = parser.parse_args()
    if args.use_cuda and torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")

    if args.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    dataset_name = args.train_dataset
    seed = args.seed
    N = args.N
    B = args.B
    epoch = args.epoch

    encoder_spec = args.encoder_spec
    load_epoch = args.load_epoch
    fine_tune_llm_q = args.tune_llm_q
    key_embd_src = args.key_embd_src
    use_data_aug = args.use_data_aug
    use_lr_decay = args.use_lr_decay
    use_cached_embd = args.use_cached_embd
    dataset_dir = args.dataset_dir
    model_dir = args.model_dir
    model_save_dir = args.model_save_dir
    log_save_dir = args.log_save_dir
    sep_query_head = args.sep_query_head
    kb_size = args.kb_size
    gradient_accm_step = args.gradient_accm_step
    if kb_size == -1:
        kb_size = None  # Progressively increase size
    elif kb_size == 0:
        kb_size = "dynamic"  # Random size

    length_invariance = args.length_invariance
    outlier_ratio = args.outlier_ratio
    multi_entities = args.multi_entities
    use_extended_qa = args.use_extended_qa
    kb_token_layer_frequency = args.kb_token_layer_frequency
    llm_type = args.llm_type
    hf_model_spec = args.hf_model_spec
    hf_token = args.hf_token

    torch.manual_seed(seed)
    np.random.seed(seed)

    pathlib.Path(model_save_dir).mkdir(parents=True, exist_ok=True)

    if args.log_to_file:
        formatter = logging.Formatter(LOGFORMAT)
        f_handler = logging.FileHandler(model_save_dir / "log.txt")
        f_handler.setFormatter(formatter)
        logger.addHandler(f_handler)

    logger.info(f"Running on {device}")

    logger.info("🚨 Started training 🚨")
    logger.info(f"💽 Saving to  {model_save_dir}💽")
    if sep_query_head:
        os.environ["SEP_QUERY_HEAD"] = "TRUE"
        logger.info("Having seperate query head for KB!")

    if length_invariance:
        os.environ["LENGTH_INVARIANCE"] = "TRUE"
        logger.info("Having seperate query head for KB!")

    os.environ["SCALE_FACTOR"] = ""

    if use_cached_embd:
        # We load the pre-computed version stored on the disk rather
        # than computing them on the fly to make things faster
        logger.info(f"Using pre-computed {encoder_spec} embedding")
        key_embds, value_embds = _load_cached_embeddigns(encoder_spec, dataset_dir, dataset_name, key_embd_src)

    logger.info(f"Experiment prefix {get_prefix_str(args)}")

    if use_extended_qa:
        dataset = json.load(open(os.path.join(dataset_dir, f"{dataset_name}_augmented.json")))
    else:
        dataset = json.load(open(os.path.join(dataset_dir, f"{dataset_name}.json")))

    training_set = dataset[:N]

    # I know it looks silly but it's for backward compatability :(
    if kb_token_layer_frequency is None:
        actual_kb_token_layer_frequency = 3
    else:
        actual_kb_token_layer_frequency = kb_token_layer_frequency

    # Set up the LLM
    llm_model_spec = model_dir if model_dir else hf_model_spec

    if llm_model_spec is None:
        raise ValueError("Either supply model_dir or hf_model_spec")

    if hf_token is None and args.llm_type == "llama3":
        raise ValueError("Please supply HuggingFace token(hf_token) when loading model Llama weights from HuggingFace")

    tokenizer = AutoTokenizer.from_pretrained(
        llm_model_spec,
        trust_remote_code=True,
        use_auth_token=hf_token if hf_token is args.llm_type == "llama3" else None,
    )
    tokenizer.pad_token = tokenizer.eos_token

    if args.llm_type == "llama3":
        model = KblamForCausalLM.from_pretrained(
            llm_model_spec,
            device_map=device,
            torch_dtype=torch.bfloat16,
            trust_remote_code=True,
            use_auth_token=hf_token,
        )
    elif args.llm_type == "phi3":
        model = KbphiForCausalLM.from_pretrained(
            llm_model_spec,
            device_map=device,
            torch_dtype="auto",
            trust_remote_code=True,
        )
    else:
        ValueError(f"LLM type {args.llm_type} not recognised")

    logger.info(model.config)  # type: ignore

    model.eval()  # type: ignore
    # freeze model
    for _, param in model.named_parameters():  # type: ignore
        param.requires_grad = False

    # Set up the encoder
    encoder = KBEncoder(
        encoder_name=encoder_spec,
        projector_type="linear",
        endpoint_url="",
        out_dim=model.config.hidden_size  # type: ignore
        * (model.config.num_hidden_layers // actual_kb_token_layer_frequency + 1),  # type: ignore
        frozen_base_model=True,
        device=device,
    )

    encoder.train()  # Double check

    kbretriever = KBRetriever(
        encoder,
        training_set,
        key_embds=key_embds,  # type: ignore
        value_embds=value_embds,  # type: ignore
    )

    logger.info("Model ready 🚀")

    # Get the training started
    lr = args.lr
    prefix_string = get_prefix_str(args)
    if fine_tune_llm_q:
        prefix_string += "FineTuneQuery"
    encoder_ckpt_name = f"{prefix_string}KeyFrom{key_embd_src}_{dataset_name}_{encoder_spec}"
    prefix_string = get_prefix_str(args)
    llm_ckpt_name = f"{prefix_string}KeyFrom{key_embd_src}_{encoder_spec}_{dataset_name}_{llm_type}"
    trainer = Trainer(
        model,  # type: ignore
        kbretriever,
        tokenizer,
        actual_kb_token_layer_frequency,
        epoch,
        lr,
        device,
        use_lr_decay,
        kb_size,  # type: ignore
        llm_ckpt_name,
        encoder_ckpt_name,
        model_save_dir,
        fine_tune_llm_q=fine_tune_llm_q,
        sep_query_head=sep_query_head,
    )

    logger.info(f"Number of trainable parameters: {_get_parameter_count(encoder):,}")

    trainer.train(
        training_set,
        B,
        gradient_accm_step,
        outlier_ratio,
        use_data_aug=use_data_aug,
        multi_entities=multi_entities,
        use_extended_qa=use_extended_qa,
        save_period=100,
        fine_tune_llm_q=fine_tune_llm_q,
    )


if __name__ == "__main__":
    main()
