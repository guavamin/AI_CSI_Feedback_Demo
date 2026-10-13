"""Empirical CDF helpers."""

from common.oai.utils.stats import empirical_cdf


def test_empirical_cdf_constant_is_vertical_step():
    xs, ys = empirical_cdf([15.0] * 50, n_points=64)
    assert xs == [15.0, 15.0]
    assert ys == [0.0, 1.0]


def test_empirical_cdf_single_sample_step():
    xs, ys = empirical_cdf([9.0], n_points=64)
    assert xs == [9.0, 9.0]
    assert ys == [0.0, 1.0]


def test_empirical_cdf_discrete_cqi_step():
    xs, ys = empirical_cdf([14.0, 15.0, 15.0, 15.0], n_points=64)
    assert xs[0] == 14.0 and ys[0] == 0.0
    assert ys[-1] == 1.0
    assert 15.0 in xs


def test_empirical_cdf_empty():
    assert empirical_cdf([]) == ([], [])
