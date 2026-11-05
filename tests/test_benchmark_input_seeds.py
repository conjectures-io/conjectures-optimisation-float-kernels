from benchmark import INPUT_SEED_STEP, paired_input_seed_key


def test_paired_input_seed_is_a_single_compact_uint32_value():
    seeds = {paired_input_seed_key() for _ in range(128)}
    assert len(seeds) == 128
    assert all(type(seed) is int and 0 <= seed < 2**32 for seed in seeds)


def test_one_seed_generates_distinct_inputs_across_samples_and_repetitions():
    base = 123456789
    inputs_per_sample = 37
    sample_seeds = [paired_input_seed_key(base, sample, inputs_per_sample) for sample in range(9)]
    generated = {
        (seed + repetition * INPUT_SEED_STEP) & 0xFFFFFFFF
        for seed in sample_seeds
        for repetition in range(inputs_per_sample)
    }
    assert len(generated) == len(sample_seeds) * inputs_per_sample
