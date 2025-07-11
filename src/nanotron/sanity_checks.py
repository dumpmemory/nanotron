from contextlib import contextmanager
from typing import Callable, Optional

import torch
from transformers import AutoTokenizer

from nanotron import distributed as dist
from nanotron import logging, optim
from nanotron.config import Config
from nanotron.logging import get_logger, log_rank
from nanotron.models import NanotronModel
from nanotron.optim.gradient_accumulator import GradientAccumulator
from nanotron.parallel import ParallelContext
from nanotron.parallel.tied_parameters import get_tied_id_to_param

logger = get_logger(__name__)


def assert_tensor_synced_across_pg(
    tensor: torch.Tensor,
    pg: dist.ProcessGroup,
    msg: Optional[Callable[[str], str]] = None,
    reference_rank: int = 0,
):
    """Assert that `tensor` is synced across `pg` with reference rank. Note that this always passes for reference rank"""
    if dist.get_rank(pg) == reference_rank:
        reference_tensor = tensor
    else:
        reference_tensor = torch.empty_like(tensor)
    dist.broadcast(
        reference_tensor,
        src=dist.get_global_rank(group=pg, group_rank=reference_rank),
        group=pg,
    )

    # TODO @nouamane: Getting Greatest absolute difference: 4.6e-10 at large scale when syncing tied weights
    torch.testing.assert_close(tensor, reference_tensor, msg=msg)


# TODO @nouamanetazi: remove this with SANITY_CHECKS
@contextmanager
def assert_fail_except_rank_with(exception_class, rank_exception, pg):
    try:
        yield
    except exception_class:
        if rank_exception == dist.get_rank(pg):
            raise AssertionError(f"Expected rank {rank_exception} to not raise {exception_class}.")
        else:
            return

    except Exception as e:
        raise AssertionError(f"Expected {exception_class} to be raised, but got {type(e)} instead:\n{e}")
    if dist.get_rank(pg) != rank_exception:
        raise AssertionError(f"Expected {exception_class} to be raised, but no exception was raised.")


def before_tbi_sanity_checks(
    config: Config,
    parallel_context: ParallelContext,
    unwrapped_model: NanotronModel,
    grad_accumulator: GradientAccumulator,
    lr_scheduler: torch.optim.lr_scheduler.LRScheduler,
) -> None:
    if not config.general.ignore_sanity_checks:
        # SANITY CHECK: Check that the model params are synchronized across dp_cp
        for name, param in sorted(unwrapped_model.named_parameters(), key=lambda x: x[0]):
            assert_tensor_synced_across_pg(
                tensor=param,
                pg=parallel_context.dp_cp_pg,
                msg=lambda err: f"{name} are not synchronized across DP_CP {err}",
            )

        # SANITY CHECK: Tied weights are synchronized
        tied_params_list = sorted(
            get_tied_id_to_param(
                parameters=unwrapped_model.parameters(),
                root_module=unwrapped_model,
            ).items(),
            key=lambda x: x[0],
        )
        for (name, group_ranks), param in tied_params_list:
            group = parallel_context.world_ranks_to_pg[group_ranks]
            assert_tensor_synced_across_pg(
                tensor=param,
                pg=group,
                msg=lambda err: f"[Before train] Tied weights {name} are not synchronized. {err}",
            )

        # SANITY CHECK: Check that model grads are zeroed or None
        for name, param in unwrapped_model.named_parameters():
            if param.grad is not None:
                torch.testing.assert_close(
                    param.grad,
                    torch.zeros_like(param.grad),
                    atol=0,
                    rtol=0,
                    msg="Model half precision grads must be zeroed or None in first accumulation step.",
                )

        # SANITY CHECK: Check that the grad accumulator buffers are ready for DDP
        if grad_accumulator is not None:
            for _, elt in grad_accumulator.fp32_grad_buffers.items():
                fp32_grad_buffer = elt["fp32_grad"]
                torch.testing.assert_close(
                    fp32_grad_buffer,
                    torch.zeros_like(fp32_grad_buffer),
                    atol=0,
                    rtol=0,
                    msg="Grad accumulator buffers must be zeroed in first accumulation step.",
                )

            # TODO: add checks for memory contiguousness

        # SANITY CHECK: Check that optimizer's lr is synchronized with lr_scheduler
        for i, group in enumerate(lr_scheduler.optimizer.param_groups):
            assert (
                group["lr"] == lr_scheduler.get_last_lr()[i]
            ), f"Optimizer and LR scheduler are not in sync. Got {group['lr']} and {lr_scheduler.get_last_lr()[i]}"
            break

        # SANITY CHECK: run model specific sanity checks
        unwrapped_model.before_tbi_sanity_checks()


def after_tbi_sanity_checks(
    config: Config,
    parallel_context: ParallelContext,
    unwrapped_model: NanotronModel,
    grad_accumulator: GradientAccumulator,
) -> None:
    if not config.general.ignore_sanity_checks:
        # SANITY CHECK: Check that gradient flow on the entire model
        # SANITY CHECK: Check that all parameters that required gradients, have actually a gradient
        # SANITY CHECK: Check for nan/inf
        for name, param in unwrapped_model.named_parameters():
            if not param.requires_grad:
                continue

            if param.is_tied:
                tied_info = param.get_tied_info()
                name = tied_info.get_full_name_from_module_id_to_prefix(
                    module_id_to_prefix=unwrapped_model.module_id_to_prefix
                )

            if grad_accumulator is not None:
                grad = grad_accumulator.get_grad_buffer(name=name)
            else:
                grad = param.grad

            if torch.isnan(grad).any() or torch.isinf(grad).any():
                raise ValueError(f"Gradient is nan or inf for {name}")
            if grad is None:
                log_rank(
                    f"Process rank { dist.get_rank(parallel_context.world_pg)}/{parallel_context.world_pg.size()}: {name} is missing gradient",
                    logger=logger,
                    level=logging.ERROR,
                )

        # SANITY CHECK: run model specific sanity checks
        unwrapped_model.after_tbi_sanity_checks()


def before_optim_step_sanity_checks(
    config: Config,
    parallel_context: ParallelContext,
    unwrapped_model: NanotronModel,
    grad_accumulator: GradientAccumulator,
    optimizer: optim.BaseOptimizer,
) -> None:
    if not config.general.ignore_sanity_checks:
        # SANITY CHECK: Test tied weights gradients are synchronized
        for (name, group_ranks), param in sorted(
            get_tied_id_to_param(parameters=unwrapped_model.parameters(), root_module=unwrapped_model).items(),
            key=lambda x: x[0],
        ):
            if not param.requires_grad:
                continue

            if grad_accumulator is not None:
                grad = grad_accumulator.get_grad_buffer(name=name)
            else:
                grad = param.grad

            assert grad is not None, f"Grad is None for {name}"
            group = parallel_context.world_ranks_to_pg[group_ranks]
            assert_tensor_synced_across_pg(
                tensor=grad,
                pg=group,
                msg=lambda err: f"[Before optimizer step] Tied weights grads for {name} are not synchronized. {err}",
            )

        # SANITY CHECK: Test gradients are synchronized across DP
        for name, param in sorted(unwrapped_model.named_parameters(), key=lambda x: x[0]):
            if not param.requires_grad:
                continue

            if param.is_tied:
                tied_info = param.get_tied_info()
                name = tied_info.get_full_name_from_module_id_to_prefix(
                    module_id_to_prefix=unwrapped_model.module_id_to_prefix
                )

            if grad_accumulator is not None:
                grad = grad_accumulator.get_grad_buffer(name=name)
            else:
                grad = param.grad

            assert grad is not None, f"Grad is None for {name}"
            assert_tensor_synced_across_pg(
                tensor=grad,
                pg=parallel_context.dp_cp_pg,
                msg=lambda err: f"[Before optimizer step] weights grads for {name} are not synchronized across DP_CP. {err}",
            )

        # SANITY CHECK: Check that the model params are synchronized across dp
        for name, param in sorted(unwrapped_model.named_parameters(), key=lambda x: x[0]):
            assert_tensor_synced_across_pg(
                tensor=param,
                pg=parallel_context.dp_cp_pg,
                msg=lambda err: f"{name} are not synchronized across DP_CP {err}",
            )

        # SANITY CHECK: Tied weights are synchronized
        tied_params_list = sorted(
            get_tied_id_to_param(parameters=unwrapped_model.parameters(), root_module=unwrapped_model).items(),
            key=lambda x: x[0],
        )

        for (name, group_ranks), param in tied_params_list:
            group = parallel_context.world_ranks_to_pg[group_ranks]
            assert_tensor_synced_across_pg(
                tensor=param,
                pg=group,
                msg=lambda err: f"[Before optimizer step] Tied weights {name} are not synchronized. {err}",
            )

        # SANITY CHECK: Check that optimizer states are synchronized across DP_CP
        check_optim_state_in_sync(optimizer.state_dict(), parallel_context.dp_cp_pg)

        # SANITY CHECK: run model specific sanity checks
        unwrapped_model.before_optim_step_sanity_checks()


def after_optim_step_sanity_checks(
    config: Config,
    parallel_context: ParallelContext,
    unwrapped_model: NanotronModel,
    grad_accumulator: GradientAccumulator,
) -> None:
    if not config.general.ignore_sanity_checks:
        # SANITY CHECK: Check that gradients is cleared
        for name, param in unwrapped_model.named_parameters():
            if not param.requires_grad:
                continue

            if param.grad is not None:
                log_rank(
                    f"Process rank { dist.get_rank(parallel_context.world_pg)}/{parallel_context.world_pg.size()}: {name} still has gradient despite having ran the optimizer",
                    logger=logger,
                    level=logging.ERROR,
                )

        # SANITY CHECK: run model specific sanity checks
        unwrapped_model.after_optim_step_sanity_checks()


def check_optim_state_in_sync(optim_state_dict: dict, pg: dist.ProcessGroup):
    for _, optim_state in sorted(optim_state_dict["state"].items(), key=lambda x: x[0]):
        for name, tensor in optim_state.items():
            if name == "step":
                continue
            assert_tensor_synced_across_pg(
                tensor=tensor, pg=pg, msg=lambda err: f"{name} are not synced across DP {err}"
            )


def sanity_check_dataloader(dataloader, tokenizer_path, sanity_check_dataloader_interval=None):
    """
    Debug function to check dataloader samples.
    Args:
        dataloader: The dataloader to check
        tokenizer_path: Path to the tokenizer
        sanity_check_dataloader_interval: Interval at which to check samples
    """
    if sanity_check_dataloader_interval is None:
        return

    NUM_BATCHES = 10
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)

    if dist.get_rank() == 0:
        check_step = -1

        with open("sanity_check.txt", "w") as f:
            f.write("")

        for i, batch in enumerate(dataloader):
            check_step += 1
            if i % sanity_check_dataloader_interval == 0:
                with open("sanity_check.txt", "a") as f:
                    f.write("\n\n")
                    f.write("*" * 40)
                    f.write(f"Sanity check {check_step}")
                    f.write("*" * 40)
                print(batch)

                texts = tokenizer.batch_decode(
                    batch["input_ids"], skip_special_tokens=False, clean_up_tokenization_spaces=False
                )

                for j, text in enumerate(texts):
                    print(f"\n\n>>Batch {i} || Sample {j}<<\n")
                    print(text[:400])
                    with open("sanity_check.txt", "a") as f:
                        f.write(f"\n\n>>Batch {i} || Sample {j}<<\n")
                        f.write(text)

                if i // sanity_check_dataloader_interval == NUM_BATCHES - 1:
                    break
        raise AssertionError("Sanity check complete - stopping training")

    dist.barrier()
