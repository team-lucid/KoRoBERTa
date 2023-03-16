# Copyright 2021 The HuggingFace Team All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import logging
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

import flax
import jax
import jax.numpy as jnp
import numpy as np
import optax
import tensorflow as tf
import wandb
from flax import jax_utils, traverse_util
from flax.training import train_state
from flax.training.common_utils import onehot, shard
from transformers import (
    AutoTokenizer,
    HfArgumentParser,
    PreTrainedTokenizerBase,
    set_seed,
)

from roberta import RobertaConfig, FlaxRobertaForMaskedLM, FlaxRobertaForRTD


@dataclass
class TrainingArguments:
    output_dir: str = field(
        metadata={
            "help": "The output directory where the model predictions and checkpoints will be written."
        },
    )
    overwrite_output_dir: bool = field(
        default=False,
        metadata={
            "help": (
                "Overwrite the content of the output directory. "
                "Use this to continue training if output_dir points to a checkpoint directory."
            )
        },
    )
    train_batch_size: int = field(
        default=8, metadata={"help": "Batch size per GPU/TPU core/CPU for training."}
    )
    gradient_accumulation_steps: int = field(
        default=1,
        metadata={
            "help": "Number of updates steps to accumulate before performing a backward/update pass."
        },
    )
    learning_rate: float = field(
        default=5e-5, metadata={"help": "The initial learning rate for AdamW."}
    )
    weight_decay: float = field(
        default=0.0, metadata={"help": "Weight decay for AdamW if we apply some."}
    )
    max_grad_norm: float = field(
        default=1.0, metadata={"help": "Max gradient norm."}
    )
    adam_beta1: float = field(
        default=0.9, metadata={"help": "Beta1 for AdamW optimizer"}
    )
    adam_beta2: float = field(
        default=0.98, metadata={"help": "Beta2 for AdamW optimizer"}
    )
    adam_epsilon: float = field(
        default=1e-6, metadata={"help": "Epsilon for AdamW optimizer."}
    )
    adamw: bool = field(
        default=True, metadata={"help": "Whether or not to replace Lamb with AdamW."}
    )
    num_train_steps: float = field(
        default=500000, metadata={"help": "Total number of training steps to perform."}
    )
    warmup_steps: int = field(
        default=10000, metadata={"help": "Linear warmup over warmup_steps."}
    )
    logging_steps: int = field(
        default=100, metadata={"help": "Log every X updates steps."}
    )
    save_steps: int = field(
        default=10000, metadata={"help": "Save checkpoint every X updates steps."}
    )
    seed: int = field(
        default=42,
        metadata={"help": "Random seed that will be set at the beginning of training."},
    )

    def __post_init__(self):
        if self.output_dir is not None:
            self.output_dir = os.path.expanduser(self.output_dir)

    def to_dict(self):
        """
        Serializes this instance while replace `Enum` by their values (for JSON serialization support). It obfuscates
        the token values by removing their value.
        """
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, Enum):
                d[k] = v.value
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], Enum):
                d[k] = [x.value for x in v]
            if k.endswith("_token"):
                d[k] = f"<{k.upper()}>"
        return


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune, or train from scratch.
    """

    generator_name_or_path: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "The model checkpoint for weights initialization.Don't set if you want to train a model from scratch."
            )
        },
    )
    generator_config_name: Optional[str] = field(
        default=None,
        metadata={
            "help": "Pretrained config name or path if not the same as model_name"
        },
    )
    discriminator_name_or_path: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "The model checkpoint for weights initialization.Don't set if you want to train a model from scratch."
            )
        },
    )
    discriminator_config_name: Optional[str] = field(
        default=None,
        metadata={
            "help": "Pretrained config name or path if not the same as model_name"
        },
    )
    tokenizer_name: Optional[str] = field(
        default=None,
        metadata={
            "help": "Pretrained tokenizer name or path if not the same as model_name"
        },
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={
            "help": "Where do you want to store the pretrained models downloaded from s3"
        },
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={
            "help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."
        },
    )
    dtype: Optional[str] = field(
        default="float32",
        metadata={
            "help": (
                "Floating-point format in which the model weights should be initialized and trained. Choose one of"
                " `[float32, float16, bfloat16]`."
            )
        },
    )


@dataclass
class DataTrainingArguments:
    dataset_name: Optional[str] = field(
        default=None,
        metadata={"help": "The name of the dataset to use (via the datasets library)."},
    )
    mlm_probability: float = field(
        default=0.15, metadata={"help": "Ratio of tokens to mask for masked language modeling loss"}
    )
    compression_type: Optional[str] = field(
        default="GZIP",
        metadata={
            "help": "The compression type of the datasets to load. "
                    "If no compression type is specified, the datasets are loaded in streaming mode."
        },
    )


@flax.struct.dataclass
class FlaxDataCollatorForMaskedLM:
    tokenizer: PreTrainedTokenizerBase
    mlm_probability: float = 0.15
    replace_prob = 0.1
    orginal_prob = 0.1

    def __post_init__(self):
        if self.tokenizer.mask_token is None:
            raise ValueError(
                "This tokenizer does not have a mask token which is necessary for masked language modeling. "
                "You should pass `mlm=False` to train on causal language modeling instead."
            )

    def __call__(self, input_ids: np.ndarray) -> Dict[str, np.ndarray]:
        # Handle dict or lists with proper padding and conversion to tensor.
        batch = {"input_ids": input_ids, "attention_mask": np.ones_like(input_ids), "original_ids": input_ids}

        special_tokens_mask = self.get_special_tokens_mask(input_ids)

        batch["input_ids"], batch["labels"], batch["masked_indices"] = self.mask_tokens(
            batch["input_ids"], special_tokens_mask=special_tokens_mask
        )
        return batch

    # get special tokens mask
    def get_special_tokens_mask(self, input_ids: np.ndarray) -> np.ndarray:
        special_tokens_mask = np.zeros_like(input_ids, dtype=np.bool_)

        for special_token in self.tokenizer.all_special_ids:
            special_tokens_mask |= input_ids == special_token

        return special_tokens_mask

    def mask_tokens(
            self, inputs: np.ndarray, special_tokens_mask: Optional[np.ndarray]
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Prepare masked tokens inputs/labels for masked language modeling: 80% MASK, 10% random, 10% original.
        """
        labels = inputs.copy()
        # We sample a few tokens in each sequence for MLM training (with probability `self.mlm_probability`)
        probability_matrix = np.full(labels.shape, self.mlm_probability)
        special_tokens_mask = special_tokens_mask.astype("bool")

        probability_matrix[special_tokens_mask] = 0.0
        masked_indices = np.random.binomial(1, probability_matrix).astype("bool")
        labels[~masked_indices] = -100  # We only compute loss on masked tokens

        # 80% of the time, we replace masked input tokens with tokenizer.mask_token ([MASK])
        indices_replaced = (
                np.random.binomial(1, np.full(labels.shape, 0.8)).astype("bool")
                & masked_indices
        )
        inputs[indices_replaced] = self.tokenizer.convert_tokens_to_ids(
            self.tokenizer.mask_token
        )

        # 10% of the time, we replace masked input tokens with random word
        indices_random = np.random.binomial(1, np.full(labels.shape, 0.5)).astype(
            "bool"
        )
        indices_random &= masked_indices & ~indices_replaced

        random_words = np.random.randint(
            self.tokenizer.vocab_size, size=labels.shape, dtype="i4"
        )
        inputs[indices_random] = random_words[indices_random]

        # The rest of the time (10% of the time) we keep the masked input tokens unchanged
        return inputs, labels, masked_indices


def main():
    parser = HfArgumentParser(
        (ModelArguments, DataTrainingArguments, TrainingArguments)
    )
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    if (
            os.path.exists(training_args.output_dir)
            and os.listdir(training_args.output_dir)
            and not training_args.overwrite_output_dir
    ):
        raise ValueError(
            f"Output directory ({training_args.output_dir}) already exists and is not empty."
            "Use --overwrite_output_dir to overcome."
        )

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        level=logging.INFO,
        datefmt="[%X]",
    )

    # Log on each process the small summary:
    logger = logging.getLogger(__name__)

    # Set the verbosity to info of the Transformers logger (on main process only):
    logger.info(f"Training/evaluation parameters {training_args}")

    # Set seed before initializing model.
    set_seed(training_args.seed)

    wandb.init(project="roberta", config=asdict(training_args))

    if model_args.generator_config_name:
        generator_config = RobertaConfig.from_pretrained(
            model_args.generator_config_name, cache_dir=model_args.cache_dir
        )
    elif model_args.generator_name_or_path:
        generator_config = RobertaConfig.from_pretrained(
            model_args.generator_name_or_path, cache_dir=model_args.cache_dir
        )
    else:
        raise ValueError(
            "You have to specify a generator_config_name or a generator_name_or_path"
        )

    if model_args.discriminator_config_name:
        discriminator_config = RobertaConfig.from_pretrained(
            model_args.discriminator_config_name, cache_dir=model_args.cache_dir
        )
    elif model_args.discriminator_name_or_path:
        discriminator_config = RobertaConfig.from_pretrained(
            model_args.discriminator_name_or_path, cache_dir=model_args.cache_dir
        )
    else:
        raise ValueError(
            "You have to specify a discriminator_config_name or a discriminator_name_or_path"
        )

    if model_args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.tokenizer_name,
            cache_dir=model_args.cache_dir,
            use_fast=model_args.use_fast_tokenizer,
        )

    data_collator = FlaxDataCollatorForMaskedLM(
        tokenizer=tokenizer, mlm_probability=data_args.mlm_probability
    )

    # Initialize our training
    rng = jax.random.PRNGKey(training_args.seed)
    dropout_rngs = jax.random.split(rng, jax.local_device_count())

    if model_args.generator_name_or_path:
        generator = FlaxRobertaForMaskedLM.from_pretrained(
            model_args.generator_name_or_path,
            config=generator_config,
            seed=training_args.seed,
            dtype=getattr(jnp, model_args.dtype),
        )
    else:
        generator = FlaxRobertaForMaskedLM(
            generator_config,
            seed=training_args.seed,
            dtype=getattr(jnp, model_args.dtype),
        )

    if model_args.discriminator_name_or_path:
        discriminator = FlaxRobertaForRTD.from_pretrained(
            model_args.discriminator_name_or_path,
            config=discriminator_config,
            seed=training_args.seed,
            dtype=getattr(jnp, model_args.dtype),
        )
    else:
        discriminator = FlaxRobertaForRTD(
            discriminator_config,
            seed=training_args.seed,
            dtype=getattr(jnp, model_args.dtype),
        )

    # Store some constant
    train_batch_size = int(training_args.train_batch_size)
    num_train_steps = int(training_args.num_train_steps)

    # use tf to get filelist from gcs
    def _parse_function(example_proto):
        features = {"text": tf.io.FixedLenFeature([512], tf.int64)}
        parsed_features = tf.io.parse_single_example(example_proto, features)
        return parsed_features["text"]

    data_files = tf.io.gfile.glob(data_args.dataset_name)
    dataset = tf.data.TFRecordDataset(data_files, compression_type=data_args.compression_type)
    dataset = dataset.map(_parse_function, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    dataset = dataset.shuffle(65536)
    dataset = dataset.batch(train_batch_size, drop_remainder=True)
    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)

    # Create learning rate schedule
    warmup_fn = optax.linear_schedule(
        init_value=0.0,
        end_value=training_args.learning_rate,
        transition_steps=training_args.warmup_steps,
    )
    decay_fn = optax.linear_schedule(
        init_value=training_args.learning_rate,
        end_value=0,
        transition_steps=num_train_steps - training_args.warmup_steps,
    )
    linear_decay_lr_schedule_fn = optax.join_schedules(
        schedules=[warmup_fn, decay_fn], boundaries=[training_args.warmup_steps]
    )

    def decay_mask_fn(params):
        flat_params = traverse_util.flatten_dict(params)
        # find out all LayerNorm parameters
        layer_norm_candidates = ["layernorm", "layer_norm", "ln"]
        layer_norm_named_params = set(
            [
                layer[-2:]
                for layer_norm_name in layer_norm_candidates
                for layer in flat_params.keys()
                if layer_norm_name in "".join(layer).lower()
            ]
        )
        flat_mask = {
            path: (path[-1] != "bias" and path[-2:] not in layer_norm_named_params)
            for path in flat_params
        }
        return traverse_util.unflatten_dict(flat_mask)

    if training_args.adamw:
        optimizer = optax.adamw(
            learning_rate=linear_decay_lr_schedule_fn,
            b1=training_args.adam_beta1,
            b2=training_args.adam_beta2,
            eps=training_args.adam_epsilon,
            weight_decay=training_args.weight_decay,
            mask=decay_mask_fn,
        )
    else:
        optimizer = optax.lamb(
            learning_rate=linear_decay_lr_schedule_fn,
            b1=training_args.adam_beta1,
            b2=training_args.adam_beta2,
            eps=training_args.adam_epsilon,
            weight_decay=training_args.weight_decay,
            mask=decay_mask_fn,
        )

    optimizer = optax.chain(
        optax.clip_by_global_norm(training_args.max_grad_norm),
        optimizer,
    )

    if training_args.gradient_accumulation_steps > 1:
        optimizer = optax.MultiSteps(
            optimizer, training_args.gradient_accumulation_steps
        )

    # copy optimizer for generator and discriminator
    generator_optimizer = copy.deepcopy(optimizer)
    discriminator_optimizer = copy.deepcopy(optimizer)

    generator_state = train_state.TrainState.create(
        apply_fn=generator.__call__, params=generator.params, tx=generator_optimizer
    )
    discriminator_state = train_state.TrainState.create(
        apply_fn=discriminator.__call__,
        params=discriminator.params,
        tx=discriminator_optimizer,
    )

    def train_step(state_g, state_d, batch, dropout_rng):
        dropout_rng, new_dropout_rng, sample_rng = jax.random.split(dropout_rng, 3)

        def generator_loss_fn(params):
            labels = batch["labels"]

            logits = state_g.apply_fn(
                batch["input_ids"],
                batch["attention_mask"],
                params=params,
                dropout_rng=dropout_rng,
                train=True,
            )[0]

            label_mask = jnp.where(labels > 0, 1.0, 0.0)
            loss = (
                    optax.softmax_cross_entropy(logits, onehot(labels, logits.shape[-1]))
                    * label_mask
            )

            loss = loss.sum()
            num_labels = label_mask.sum()

            return loss, (logits, num_labels)

        def discriminator_loss_fn(params):
            input_ids = batch["pred_ids"]
            labels = batch["replaced_ids"].astype("float32")

            input_embeds = jnp.take(
                params["roberta"]["embeddings"]["word_embeddings"]["embedding"],
                input_ids,
                axis=0,
            ) + jnp.take(
                state_g.params["roberta"]["embeddings"]["word_embeddings"]["embedding"],
                input_ids,
                axis=0,
            )
            input_embeds = input_embeds / 2

            logits = state_d.apply_fn(
                input_ids=input_ids,
                attention_mask=batch["attention_mask"],
                inputs_embeds=input_embeds,
                params=params,
                dropout_rng=dropout_rng,
                train=True,
            )[0]

            label_mask = jnp.ones_like(labels)
            loss = optax.sigmoid_binary_cross_entropy(logits, labels)

            loss = loss.sum() * 50
            num_labels = label_mask.sum()

            return loss, (logits, num_labels)

        generator_grad_fn = jax.value_and_grad(generator_loss_fn, has_aux=True)
        (generator_loss, (logits, num_labels)), generator_grad = generator_grad_fn(
            state_g.params
        )
        num_labels_g = jax.lax.psum(num_labels, "batch")

        generator_loss = jax.lax.psum(generator_loss, "batch")
        generator_loss = jax.tree_util.tree_map(
            lambda x: x / num_labels_g, generator_loss
        )

        generator_grad = jax.lax.psum(generator_grad, "batch")
        generator_grad = jax.tree_util.tree_map(
            lambda x: x / num_labels_g, generator_grad
        )
        new_state_g = state_g.apply_gradients(grads=generator_grad)

        gumbel = jax.random.gumbel(rng, logits.shape)
        pred_ids = jnp.argmax(logits + gumbel, axis=-1)
        pred_ids = jnp.where(batch["masked_indices"], pred_ids, batch["input_ids"])

        batch["pred_ids"] = pred_ids
        batch["replaced_ids"] = (pred_ids != batch["original_ids"])

        discriminator_grad_fn = jax.value_and_grad(discriminator_loss_fn, has_aux=True)
        (
            discriminator_loss,
            (logits, num_labels),
        ), discriminator_grad = discriminator_grad_fn(state_d.params)
        num_labels_d = jax.lax.psum(num_labels, "batch")

        discriminator_loss = jax.lax.psum(discriminator_loss, "batch")
        discriminator_loss = jax.tree_util.tree_map(
            lambda x: x / num_labels_d, discriminator_loss
        )

        discriminator_grad = jax.lax.psum(discriminator_grad, "batch")
        discriminator_grad = jax.tree_util.tree_map(
            lambda x: x / num_labels_d, discriminator_grad
        )
        new_state_d = state_d.apply_gradients(
            grads=discriminator_grad
        )

        metrics = {
            "generator_loss": generator_loss,
            "discriminator_loss": discriminator_loss,
        }

        return new_state_g, new_state_d, metrics, new_dropout_rng

    p_train_step = jax.pmap(train_step, axis_name="batch", donate_argnums=(0, 1,))

    generator_state = jax_utils.replicate(generator_state)
    discriminator_state = jax_utils.replicate(discriminator_state)

    print("***** Running training *****")
    cur_step = 0
    minibatch_idx = 0
    while cur_step < num_train_steps:
        for batch in dataset:
            batch = data_collator(batch.numpy())
            batch = shard(batch)

            (
                generator_state,
                discriminator_state,
                train_metric,
                dropout_rngs,
            ) = p_train_step(generator_state, discriminator_state, batch, dropout_rngs)

            minibatch_idx += 1
            if minibatch_idx != training_args.gradient_accumulation_steps:
                continue
            minibatch_idx = 0

            cur_step += 1
            if cur_step % training_args.logging_steps == 0:
                train_metric = jax_utils.unreplicate(train_metric)
                wandb.log(
                    {
                        "loss": float(train_metric["loss"]),
                    },
                    step=cur_step,
                )

            if cur_step % training_args.save_steps == 0:
                if jax.process_index() == 0:
                    outdir = os.path.join(
                        training_args.output_dir, f"checkpoint-{cur_step}"
                    )

                    generator_params = jax.device_get(
                        jax.tree_util.tree_map(lambda x: x[0], generator_state.params)
                    )
                    discriminator_params = jax.device_get(
                        jax.tree_util.tree_map(
                            lambda x: x[0], discriminator_state.params
                        )
                    )

                    generator.save_pretrained(
                        f"{outdir}/generator", params=generator_params
                    )
                    discriminator.save_pretrained(
                        f"{outdir}/discriminator", params=discriminator_params
                    )

                    tokenizer.save_pretrained(outdir)

            if cur_step >= num_train_steps:
                break


if __name__ == "__main__":
    main()
