# from typing import List
# from datasets.arrow_dataset import Dataset
# from datasets.dataset_dict import DatasetDict, IterableDatasetDict
# from datasets.iterable_dataset import IterableDataset

# import pytest
# import torch
# from datasets import load_dataset
# from helpers.utils import init_distributed
# from nanotron import distributed as dist
# from nanotron.doremi.dataloader import DistributedSamplerForDoReMi
# from nanotron.doremi.doremi_context import DoReMiContext
# from nanotron.parallel import ParallelContext
# from torch.utils.data import Dataset


import pytest
import torch
from datasets import load_dataset
from helpers.utils import init_distributed
from nanotron import distributed as dist
from nanotron.doremi.dataloader import DistributedSamplerForDoReMi
from nanotron.doremi.doremi_context import DoReMiContext
from nanotron.parallel import ParallelContext


@pytest.fixture
def dataset1():
    return load_dataset("stas/c4-en-10k", split="train")


@pytest.fixture
def dataset2():
    return load_dataset("stas/openwebtext-synthetic-testing", split="10.repeat")


@pytest.fixture
def datasets(dataset1, dataset2):
    return [dataset1, dataset2]


def test_dist_doremi_sampler_sync_across_tp(datasets: list):
    num_microbatches = 32
    batch_size = 16
    domain_weights = torch.tensor([0.7, 0.3])
    domain_keys = [f"domain {i}" for i in range(len(datasets))]
    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)

    init_distributed(tp=2, dp=1, pp=1)(_test_dist_doremi_sampler_sync_across_tp)(
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        datasets=datasets,
        doremi_context=doremi_context,
    )


def _test_dist_doremi_sampler_sync_across_tp(
    parallel_context: ParallelContext, batch_size: int, num_microbatches: int, datasets, doremi_context: DoReMiContext
):
    dp_size = dist.get_world_size(parallel_context.dp_pg)
    dp_rank = dist.get_rank(parallel_context.dp_pg)

    sampler = DistributedSamplerForDoReMi(
        datasets,
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        num_replicas=dp_size,
        rank=dp_rank,
        doremi_context=doremi_context,
        parallel_context=parallel_context,
    )

    tp_size = dist.get_world_size(parallel_context.tp_pg)
    yield_idxs = torch.tensor(list(sampler), device="cuda").view(-1)
    gathered_idxs = [torch.empty_like(yield_idxs, device="cuda") for _ in range(tp_size)]
    dist.all_gather(gathered_idxs, yield_idxs)
    assert all(torch.allclose(t1, t2) for t1, t2 in zip(gathered_idxs, gathered_idxs[1:]))


def test_dist_doremi_sampler_not_overlapse_across_dp(datasets: list):
    num_microbatches = 32
    batch_size = 16
    domain_weights = torch.tensor([0.7, 0.3])
    domain_keys = [f"domain {i}" for i in range(len(datasets))]
    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)

    init_distributed(tp=1, dp=2, pp=1)(_test_dist_doremi_sampler_not_overlapse_across_dp)(
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        datasets=datasets,
        doremi_context=doremi_context,
    )


def _test_dist_doremi_sampler_not_overlapse_across_dp(
    parallel_context: ParallelContext, batch_size: int, num_microbatches: int, datasets, doremi_context: DoReMiContext
):
    dp_size = dist.get_world_size(parallel_context.dp_pg)
    dp_rank = dist.get_rank(parallel_context.dp_pg)

    sampler = DistributedSamplerForDoReMi(
        datasets,
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        num_replicas=dp_size,
        rank=dp_rank,
        doremi_context=doremi_context,
        parallel_context=parallel_context,
    )

    yield_idxs = torch.tensor(list(sampler), device="cuda").view(-1)
    gathered_idxs = [torch.empty_like(yield_idxs, device="cuda") for _ in range(dp_size)]
    dist.all_gather(gathered_idxs, yield_idxs)
    assert not torch.any(torch.isin(*gathered_idxs))


@pytest.mark.parametrize(
    "domain_weights",
    [
        torch.tensor([0.6, 0.4]),
        # NOTE: test auto fill samples if there are rounding errors
        # the last domain results in no sample (round(0.004 * 64) = 0)
        # but if do with global batch size, (round(0.004 * 512) = 2)
        torch.tensor([0.498, 0.498, 0.004]),
        torch.tensor(
            [
                0.34356916553540745,
                0.16838812972610234,
                0.24711766854236725,
                0.0679225638705455,
                0.059079828519653675,
                0.043720261601881555,
                0.01653850841342608,
                0.00604146633842096,
                0.04342813428189645,
                0.0041942731702987,
            ]
        ),
    ],
)
def test_determistic_doremi_sampler(domain_weights, dataset1):
    num_microbatches = 32
    batch_size = 16
    datasets = [dataset1 for _ in range(len(domain_weights))]
    domain_keys = [f"domain {i}" for i in range(len(domain_weights))]
    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)
    n_epochs = 3

    init_distributed(tp=1, dp=1, pp=1)(_test_determistic_doremi_sampler)(
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        datasets=datasets,
        doremi_context=doremi_context,
        n_epochs=n_epochs,
    )


def _test_determistic_doremi_sampler(
    parallel_context: ParallelContext,
    batch_size: int,
    num_microbatches: int,
    n_epochs: int,
    datasets,
    doremi_context: DoReMiContext,
):
    dp_size = dist.get_world_size(parallel_context.dp_pg)
    dp_rank = dist.get_rank(parallel_context.dp_pg)

    sampler = DistributedSamplerForDoReMi(
        datasets,
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        num_replicas=dp_size,
        rank=dp_rank,
        doremi_context=doremi_context,
        parallel_context=parallel_context,
    )

    idxs_per_epoch = []
    for _ in range(n_epochs):
        all_idxs = []
        for idxs in sampler:
            all_idxs.append(idxs)

        idxs_per_epoch.append(all_idxs)
        sampler.reset()

    # NOTE: check if the sequence of idxs across epochs are all the same
    assert all(
        all(arr1[i] == arr2[i] for i in range(len(arr1))) for arr1, arr2 in zip(idxs_per_epoch, idxs_per_epoch[1:])
    )


@pytest.mark.parametrize(
    "domain_weights",
    [
        torch.tensor([0.6, 0.4]),
        # NOTE: test auto fill samples if there are rounding errors
        torch.tensor([0.296, 0.201, 0.501]),
        # NOTE: if sampling based on batch size, then
        # the last domain results in no sample (round(0.004 * 64) = 0)
        # but if do with global batch size, (round(0.004 * 512) = 2)
        torch.tensor([0.498, 0.498, 0.004]),
        torch.tensor(
            [
                0.34356916553540745,
                0.16838812972610234,
                0.24711766854236725,
                0.0679225638705455,
                0.059079828519653675,
                0.043720261601881555,
                0.01653850841342608,
                0.00604146633842096,
                0.04342813428189645,
                0.0041942731702987,
            ]
        ),
    ],
)
@pytest.mark.parametrize("dp_size", [1, 2, 4])
def test_sampling_from_dist_doremi_sampler_with_global_batch_size(dp_size, domain_weights: torch.Tensor, dataset1):
    global_batch_size = 512
    num_microbatches = 32
    batch_size = global_batch_size // (num_microbatches * dp_size)
    datasets = [dataset1 for _ in range(len(domain_weights))]
    domain_keys = [f"domain {i}" for i in range(len(datasets))]
    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)

    init_distributed(tp=1, dp=dp_size, pp=1)(_test_sampling_from_dist_doremi_sampler_with_global_batch_size)(
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        global_batch_size=global_batch_size,
        datasets=datasets,
        doremi_context=doremi_context,
    )


def _test_sampling_from_dist_doremi_sampler_with_global_batch_size(
    parallel_context: ParallelContext,
    batch_size: int,
    num_microbatches: int,
    global_batch_size: int,
    datasets,
    doremi_context: DoReMiContext,
):
    dp_size = dist.get_world_size(parallel_context.dp_pg)
    dp_rank = dist.get_rank(parallel_context.dp_pg)

    sampler = DistributedSamplerForDoReMi(
        datasets,
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        num_replicas=dp_size,
        rank=dp_rank,
        doremi_context=doremi_context,
        parallel_context=parallel_context,
    )

    domain_weights = doremi_context.domain_weights
    global_batch_size_per_domain = [round(global_batch_size * weight.item()) for weight in domain_weights]

    loop = 0
    microbatch_idx = 0
    num_samples_per_domain = [0 for _ in range(len(domain_weights))]
    yielded_idxs = []
    num_yielded_idxs = 0
    for idxs in sampler:
        assert batch_size == len(idxs)

        # NOTE: make sure the indicies from a batch
        # is proportion to the domain weights
        start_indices = [sum([len(ds) for ds in datasets[:i]]) for i in range(len(datasets))]
        end_indices = [sum([len(ds) for ds in datasets[: i + 1]]) for i in range(len(datasets))]
        for domain_idx in range(len(domain_weights)):
            num_samples = sum(1 for idx in idxs if idx >= start_indices[domain_idx] and idx < end_indices[domain_idx])
            num_samples_per_domain[domain_idx] += num_samples

        if microbatch_idx == num_microbatches - 1:
            # NOTE: if this is the last microbatch => we iterate through all the microbatches
            # now we check if the overall number of samples in each domain is correct across
            # all the microbatches
            num_samples_per_domain = torch.tensor(num_samples_per_domain, dtype=torch.int, device="cuda")

            # NOTE: the domain weights are chosen so that we expect
            # no domains have zero sample in the global batch size
            dist.all_reduce(num_samples_per_domain, op=dist.ReduceOp.SUM)
            assert (num_samples_per_domain == 0).sum().item() == 0

            for expected_bs, bs in zip(global_batch_size_per_domain, num_samples_per_domain):
                # NOTE: take into account rounding errors
                # accross all the dp ranks
                assert abs(expected_bs - bs) <= dp_size, f"abs(expected_bs - bs): {abs(expected_bs - bs)}"

            microbatch_idx = 0
            num_samples_per_domain = [0 for _ in range(len(domain_weights))]
        else:
            microbatch_idx += 1

        loop += 1
        num_yielded_idxs += len(idxs)
        yielded_idxs.extend(idxs)

    num_yielded_idxs = torch.tensor(num_yielded_idxs, dtype=torch.int, device="cuda")
    local_num_yielded_idxs = num_yielded_idxs.clone()
    dist.all_reduce(num_yielded_idxs, op=dist.ReduceOp.SUM)
    expected_num_samples = sum([round(len(ds) * weight.item()) for ds, weight in zip(datasets, domain_weights)])
    assert (
        expected_num_samples == num_yielded_idxs
    ), f"num_yielded_idxs: {num_yielded_idxs}, expected_num_samples: {expected_num_samples}, loop: {loop}, local_num_yielded_idxs: {local_num_yielded_idxs}"


@pytest.mark.parametrize(
    "domain_weights",
    [
        torch.tensor([0.6, 0.4]),
        # NOTE: test auto fill samples if there are rounding errors
        torch.tensor([0.296, 0.201, 0.501]),
        # NOTE: if sampling based on batch size, then
        # the last domain results in no sample (round(0.004 * 64) = 0)
        # but if do with global batch size, (round(0.004 * 512) = 2)
        torch.tensor([0.498, 0.498, 0.004]),
        torch.tensor(
            [
                0.34356916553540745,
                0.16838812972610234,
                0.24711766854236725,
                0.0679225638705455,
                0.059079828519653675,
                0.043720261601881555,
                0.01653850841342608,
                0.00604146633842096,
                0.04342813428189645,
                0.0041942731702987,
            ]
        ),
    ],
)
@pytest.mark.parametrize("dp_size", [1, 2, 4])
def test_dist_doremi_sampler_not_repeating_samples(domain_weights, dp_size, dataset1):
    global_batch_size = 512
    num_microbatches = 32
    batch_size = global_batch_size // (num_microbatches * dp_size)
    datasets = [dataset1 for _ in range(len(domain_weights))]
    domain_keys = [f"domain {i}" for i in range(len(datasets))]
    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)

    init_distributed(tp=1, dp=dp_size, pp=1)(_test_dist_doremi_sampler_not_repeating_samples)(
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        datasets=datasets,
        doremi_context=doremi_context,
    )


def _test_dist_doremi_sampler_not_repeating_samples(
    parallel_context: ParallelContext,
    batch_size: int,
    num_microbatches: int,
    datasets,
    doremi_context: DoReMiContext,
):
    dp_size = dist.get_world_size(parallel_context.dp_pg)
    dp_rank = dist.get_rank(parallel_context.dp_pg)

    sampler = DistributedSamplerForDoReMi(
        datasets,
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        num_replicas=dp_size,
        rank=dp_rank,
        doremi_context=doremi_context,
        parallel_context=parallel_context,
    )

    yielded_idxs = []
    for idxs in sampler:
        # NOTE: check that the indicies are not repeated
        assert not set(idxs).intersection(yielded_idxs)

        # NOTE: gather all the indicies from all the dp ranks
        idxs = torch.tensor(idxs, dtype=torch.int, device="cuda")
        all_idxs = [torch.zeros_like(idxs) for _ in range(dp_size)]
        dist.all_gather(all_idxs, idxs)
        all_idxs = torch.cat(all_idxs, dim=0).view(-1).cpu().tolist()
        yielded_idxs.extend(all_idxs)

    assert len(set(yielded_idxs)) == len(yielded_idxs)
