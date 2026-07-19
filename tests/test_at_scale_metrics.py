from evals.at_scale.metrics import latency_stats, throughput_per_minute


class TestLatencyStats:
    def test_empty_samples_returns_zeros(self):
        assert latency_stats([]) == {"min": 0.0, "p50": 0.0, "p99": 0.0, "max": 0.0}

    def test_single_sample(self):
        assert latency_stats([0.25]) == {"min": 0.25, "p50": 0.25, "p99": 0.25, "max": 0.25}

    def test_min_and_max_from_multiple_samples(self):
        result = latency_stats([0.1, 0.5, 0.2, 0.9, 0.3])
        assert result["min"] == 0.1
        assert result["max"] == 0.9

    def test_p50_is_median_of_sorted_samples(self):
        result = latency_stats([0.1, 0.2, 0.3, 0.4, 0.5])
        assert result["p50"] == 0.3

    def test_p99_is_near_max_for_large_sample_set(self):
        samples = [i / 1000 for i in range(1, 101)]  # 0.001..0.100
        result = latency_stats(samples)
        assert result["p99"] >= 0.098


class TestThroughputPerMinute:
    def test_zero_elapsed_returns_zero(self):
        assert throughput_per_minute(10, 0.0) == 0.0

    def test_negative_elapsed_returns_zero(self):
        assert throughput_per_minute(10, -1.0) == 0.0

    def test_computes_commits_per_minute(self):
        assert throughput_per_minute(60, 30.0) == 120.0
