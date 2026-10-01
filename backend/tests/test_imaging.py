import copy


def test_cache_key_changes_with_matrix_algorithm_and_level(env):
    imaging = env["imaging"]
    base = dict(
        image_digest="a" * 64,
        matrix_digest="b" * 64,
        algorithm="nnls",
        algorithm_params={},
        frozen_digest="c" * 64,
        level=0,
        x=1,
        y=2,
        kind="component",
    )
    key = imaging.tile_cache_key(**base)
    changed_matrix = imaging.tile_cache_key(**{**base, "matrix_digest": "d" * 64})
    changed_algorithm = imaging.tile_cache_key(**{**base, "algorithm": "fista_nnls"})
    changed_level = imaging.tile_cache_key(**{**base, "level": 1})
    changed_kind = imaging.tile_cache_key(**{**base, "kind": "residual"})
    assert len({key, changed_matrix, changed_algorithm, changed_level, changed_kind}) == 5
    # Parameter order must not produce accidental collisions.
    swapped = copy.deepcopy(base)
    swapped["algorithm_params"] = {"b": 2, "a": 1}
    key2 = imaging.tile_cache_key(**{**base, "algorithm_params": {"a": 1, "b": 2}})
    assert imaging.tile_cache_key(**swapped) == key2


def test_ill_conditioned_and_missing_channel_are_rejected(env):
    imaging = env["imaging"]
    matrix = {
        "channel_names": ["A", "B"],
        "coefficients": [[1.0, 1.0], [1.0, 1.0000001]],
    }
    image = {"channel_names": ["A", "B"]}
    try:
        imaging.validate_for_image(matrix, image, condition_limit=1e4)
    except imaging.UnmixValidationError as exc:
        assert exc.code == "ill_conditioned_matrix"
    else:
        raise AssertionError("ill-conditioned matrix was accepted")

    try:
        imaging.validate_for_image(
            {"channel_names": ["A"], "coefficients": [[1.0, 1.0]]},
            {"channel_names": ["A", "B"]},
        )
    except imaging.UnmixValidationError as exc:
        assert exc.code == "missing_channel"
    else:
        raise AssertionError("missing channel was accepted")
