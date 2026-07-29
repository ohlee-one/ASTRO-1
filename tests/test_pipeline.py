"""Tests unitaires pour les fonctions pures du pipeline.

On teste uniquement les fonctions qui ne nécessitent pas d'exécuter Siril ou
GraXpert : génération de script, validation de profils, construction de
commandes, etc. Les appels subprocess sont mockés quand nécessaire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astro_pipeline.config import (
    Calibration,
    Color,
    Denoise,
    Export,
    HaOIIIComposition,
    Post,
    Processing,
    Profile,
    Sensor,
    SetupProfile,
    StarNet,
    StarReduction,
    Stretch,
    Sharpening,
    Stacking,
    Registration,
    TargetProfile,
    Folders,
    BackgroundExtraction,
    ValidationError,
    load_profile,
)
from astro_pipeline.engines import siril
from astro_pipeline.engines.siril import (
    build_script,
    build_post_script,
    _rejection_arguments,
    _stretch_lines,
    _color_lines,
    _starnet_lines,
    _sharpening_lines,
    _export_lines,
)


# ---------------------------------------------------------------------------
# Fixtures : profils de test
# ---------------------------------------------------------------------------


def _make_profile(
    mode: str = "rgb",
    use_biases: bool = False,
    use_premade_masters: bool = False,
    stretch_method: str = "autostretch",
    starnet_enabled: bool = True,
) -> Profile:
    """Construit un profil de test sans passer par le YAML."""
    setup = SetupProfile(
        name="Test Setup",
        sensor=Sensor(),
        folders=Folders(),
        calibration=Calibration(
            use_biases=use_biases,
            use_premade_masters=use_premade_masters,
        ),
    )
    target = TargetProfile(
        name="Test Target",
        processing=Processing(mode=mode),
        stacking=Stacking(),
        registration=Registration(),
        post=Post(
            stretch=Stretch(method=stretch_method),
            starnet=StarNet(enabled=starnet_enabled),
            color=Color(),
            sharpening=Sharpening(),
            export=Export(),
        ),
    )
    return Profile(setup=setup, target=target)


# ---------------------------------------------------------------------------
# Tests : config.py (validation Pydantic)
# ---------------------------------------------------------------------------


class TestConfigValidation:
    """Vérifie que les modèles Pydantic valident correctement les valeurs."""

    def test_stretch_defaults(self):
        stretch = Stretch()
        assert stretch.enabled is True
        assert stretch.method == "autostretch"
        assert stretch.linked is True

    def test_stretch_asinh_validation(self):
        stretch = Stretch(method="asinh", stretch_factor=500.0)
        assert stretch.stretch_factor == 500.0

    def test_stretch_factor_out_of_range(self):
        with pytest.raises(ValidationError):
            Stretch(stretch_factor=0.5)

    def test_stretch_factor_too_high(self):
        with pytest.raises(ValidationError):
            Stretch(stretch_factor=2000.0)

    def test_starnet_defaults(self):
        starnet = StarNet()
        assert starnet.enabled is True
        assert starnet.upscale is False
        assert starnet.stretch_linear is False

    def test_color_defaults(self):
        color = Color()
        assert color.rmgreen is True
        assert color.rmgreen_type == "average"
        assert color.saturation_boost == 0.8
        assert color.saturation_threshold == 1.0
        assert color.hue_range == 6

    def test_color_rmgreen_type_invalid(self):
        with pytest.raises(ValidationError):
            Color(rmgreen_type="invalid")

    def test_sharpening_defaults(self):
        sharp = Sharpening()
        assert sharp.method == "unsharp"
        assert sharp.sigma == 1.0
        assert sharp.amount == 0.5

    def test_sharpening_wavelet(self):
        sharp = Sharpening(method="wavelet", layers=3, weights=[1.0, 0.5, 0.2])
        assert sharp.layers == 3
        assert len(sharp.weights) == 3

    def test_export_defaults(self):
        export = Export()
        assert export.enabled is True
        assert export.format == "tiff"
        assert export.deflate is True

    def test_export_format_invalid(self):
        with pytest.raises(ValidationError):
            Export(format="bmp")

    def test_haoiii_composition_defaults(self):
        comp = HaOIIIComposition()
        assert comp.enabled is True
        assert comp.use_ha_as_luminance is True

    def test_star_reduction_defaults(self):
        sr = StarReduction()
        assert sr.enabled is True
        assert sr.amount == 0.5

    def test_star_reduction_amount_out_of_range(self):
        with pytest.raises(ValidationError):
            StarReduction(amount=1.5)

    def test_post_has_all_sections(self):
        post = Post()
        assert hasattr(post, "background_extraction")
        assert hasattr(post, "denoise")
        assert hasattr(post, "starnet")
        assert hasattr(post, "stretch")
        assert hasattr(post, "haoiii_composition")
        assert hasattr(post, "color")
        assert hasattr(post, "sharpening")
        assert hasattr(post, "star_reduction")
        assert hasattr(post, "export")


# ---------------------------------------------------------------------------
# Tests : siril.py — génération de script phase 1
# ---------------------------------------------------------------------------


class TestBuildScript:
    """Vérifie que build_script génère les bonnes commandes Siril."""

    def test_rgb_mode_generates_register_and_stack(self):
        profile = _make_profile(mode="rgb")
        script = build_script(Path("/fake/session"), profile)
        assert "register pp_light" in script
        assert "stack r_pp_light" in script
        assert "seqextract_HaOIII" not in script

    def test_haoiii_mode_generates_extract(self):
        profile = _make_profile(mode="haoiii")
        script = build_script(Path("/fake/session"), profile)
        assert "seqextract_HaOIII" in script
        assert "register Ha_pp_light" in script
        assert "register OIII_pp_light" in script

    def test_premade_masters_no_stacking(self, monkeypatch):
        profile = _make_profile(use_premade_masters=True)

        # Mock find_premade_master pour éviter de chercher de vrais fichiers
        def fake_find(session_dir, folder):
            return Path(f"/fake/session/{folder}/Master_{folder}.fit")

        monkeypatch.setattr(siril, "find_premade_master", fake_find)
        script = build_script(Path("/fake/session"), profile)
        assert "stack bias" not in script
        assert "stack dark" not in script

    def test_no_biases_no_bias_block(self):
        profile = _make_profile(
            use_biases=False,
            use_premade_masters=False,
        )
        script = build_script(Path("/fake/session"), profile)
        assert "Master offset" not in script
        assert "calibrate flat -bias=" not in script

    def test_min_stars_not_injected_siril_has_no_direct_option(self):
        # Siril n'a pas d'argument direct pour imposer un nombre minimum
        # d'étoiles dans la commande register. Le champ min_stars est
        # conservé dans le profil pour documentation et validation future.
        profile = _make_profile()
        profile.target.registration.min_stars = 15
        script = build_script(Path("/fake/session"), profile)
        # On vérifie juste que le script se génère sans erreur
        assert "calibrate light" in script

    def test_requires_version_present(self):
        profile = _make_profile()
        script = build_script(Path("/fake/session"), profile)
        assert "requires 1.2.0" in script

    def test_haoiii_no_debayer(self):
        profile = _make_profile(mode="haoiii")
        script = build_script(Path("/fake/session"), profile)
        assert "-debayer" not in script

    def test_rgb_has_debayer(self):
        profile = _make_profile(mode="rgb")
        script = build_script(Path("/fake/session"), profile)
        assert "-debayer" in script


# ---------------------------------------------------------------------------
# Tests : siril.py — génération de script phase 2 (post-traitement)
# ---------------------------------------------------------------------------


class TestBuildPostScript:
    """Vérifie que build_post_script génère les bonnes commandes de post-traitement."""

    def test_rgb_mode_generates_stretch_and_export(self):
        profile = _make_profile(mode="rgb")
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "autostretch" in script
        assert "savetif" in script

    def test_haoiii_mode_no_rgbcomp_in_post_script(self):
        # La recomposition rgbcomp se fait maintenant dans compose_linear.ssf,
        # pas dans post_processing.ssf qui ne fait que stretch + couleur + export.
        profile = _make_profile(mode="haoiii")
        stacked = [Path("/fake/output/composed_linear_denoised.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "rgbcomp" not in script
        assert "autostretch" in script or "asinh" in script
        assert "savetif" in script

    def test_haoiii_with_luminance_in_compose_script(self):
        # La recomposition avec luminance se fait dans compose_linear.ssf.
        from astro_pipeline.engines.siril import build_compose_linear_script
        profile = _make_profile(mode="haoiii")
        profile.target.post.haoiii_composition.use_ha_as_luminance = True
        processed = [
            Path("/fake/output/Ha_result_bg.fits"),
            Path("/fake/output/OIII_result_bg.fits"),
        ]
        script = build_compose_linear_script(Path("/fake/session"), profile, processed)
        assert "-lum=" in script
        assert "composed_linear" in script

    def test_haoiii_without_luminance_in_compose_script(self):
        from astro_pipeline.engines.siril import build_compose_linear_script
        profile = _make_profile(mode="haoiii")
        profile.target.post.haoiii_composition.use_ha_as_luminance = False
        processed = [
            Path("/fake/output/Ha_result_bg.fits"),
            Path("/fake/output/OIII_result_bg.fits"),
        ]
        script = build_compose_linear_script(Path("/fake/session"), profile, processed)
        assert "-lum=" not in script
        assert "rgbcomp" in script

    def test_stretch_asinh_method(self):
        profile = _make_profile(stretch_method="asinh")
        profile.target.post.stretch.stretch_factor = 200.0
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "asinh" in script
        assert "200.0" in script
        assert "autostretch" not in script

    def test_starnet_command_generated(self):
        profile = _make_profile(starnet_enabled=True)
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "starnet" in script

    def test_starnet_disabled(self):
        profile = _make_profile(starnet_enabled=False)
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        # "starnet" peut apparaître dans un commentaire, mais pas comme commande
        lines = [l.strip() for l in script.split("\n") if l.strip()]
        starnet_commands = [l for l in lines if l.startswith("starnet")]
        assert len(starnet_commands) == 0

    def test_color_rmgreen_generated(self):
        profile = _make_profile()
        profile.target.post.color.rmgreen = True
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "rmgreen" in script

    def test_sharpening_unsharp_generated(self):
        profile = _make_profile()
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "unsharp" in script

    def test_sharpening_wavelet_generated(self):
        profile = _make_profile()
        profile.target.post.sharpening.method = "wavelet"
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "wavelet" in script
        assert "wrecons" in script

    def test_export_tiff(self):
        profile = _make_profile()
        profile.target.post.export.format = "tiff"
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "savetif" in script

    def test_export_png(self):
        profile = _make_profile()
        profile.target.post.export.format = "png"
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "savepng" in script

    def test_export_disabled(self):
        profile = _make_profile()
        profile.target.post.export.enabled = False
        stacked = [Path("/fake/output/result.fit")]
        script = build_post_script(Path("/fake/session"), profile, stacked)
        assert "savetif" not in script
        assert "savepng" not in script
        assert "savejpg" not in script


# ---------------------------------------------------------------------------
# Tests : siril.py — fonctions auxiliaires
# ---------------------------------------------------------------------------


class TestRejectionArguments:
    """Vérifie la traduction des réglages de rejet en syntaxe Siril."""

    def test_winsorized(self):
        profile = _make_profile()
        profile.target.stacking.rejection = "winsorized"
        profile.target.stacking.sigma_low = 3.0
        profile.target.stacking.sigma_high = 2.5
        result = _rejection_arguments(profile)
        assert "rej w 3.0 2.5" in result

    def test_linear(self):
        profile = _make_profile()
        profile.target.stacking.rejection = "linear"
        result = _rejection_arguments(profile)
        assert "rej l" in result

    def test_percentile(self):
        profile = _make_profile()
        profile.target.stacking.rejection = "percentile"
        result = _rejection_arguments(profile)
        assert "rej p" in result

    def test_none_returns_mean(self):
        profile = _make_profile()
        profile.target.stacking.rejection = "none"
        result = _rejection_arguments(profile)
        assert result == "mean"


# ---------------------------------------------------------------------------
# Tests : siril.py — fonctions de post-traitement individuelles
# ---------------------------------------------------------------------------


class TestStretchLines:
    def test_autostretch_linked(self):
        profile = _make_profile()
        profile.target.post.stretch.method = "autostretch"
        profile.target.post.stretch.linked = True
        lines = _stretch_lines(profile)
        assert any("autostretch -linked" in l for l in lines)

    def test_autostretch_unlinked(self):
        profile = _make_profile()
        profile.target.post.stretch.method = "autostretch"
        profile.target.post.stretch.linked = False
        lines = _stretch_lines(profile)
        assert any("autostretch" in l for l in lines)
        assert not any("-linked" in l for l in lines)

    def test_asinh_human(self):
        profile = _make_profile()
        profile.target.post.stretch.method = "asinh"
        profile.target.post.stretch.human = True
        lines = _stretch_lines(profile)
        assert any("asinh -human" in l for l in lines)

    def test_stretch_disabled(self):
        profile = _make_profile()
        profile.target.post.stretch.enabled = False
        lines = _stretch_lines(profile)
        assert lines == []


class TestColorLines:
    def test_rmgreen_average(self):
        profile = _make_profile()
        profile.target.post.color.rmgreen = True
        profile.target.post.color.rmgreen_type = "average"
        lines = _color_lines(profile)
        assert any("rmgreen" in l for l in lines)

    def test_color_disabled(self):
        profile = _make_profile()
        profile.target.post.color.enabled = False
        lines = _color_lines(profile)
        assert lines == []


class TestStarnetLines:
    def test_starnet_upscale(self):
        profile = _make_profile()
        profile.target.post.starnet.upscale = True
        lines = _starnet_lines(profile)
        assert any("-upscale" in l for l in lines)

    def test_starnet_disabled(self):
        profile = _make_profile()
        profile.target.post.starnet.enabled = False
        lines = _starnet_lines(profile)
        assert lines == []


class TestSharpeningLines:
    def test_unsharp(self):
        profile = _make_profile()
        profile.target.post.sharpening.method = "unsharp"
        profile.target.post.sharpening.sigma = 1.5
        profile.target.post.sharpening.amount = 0.8
        lines = _sharpening_lines(profile)
        assert any("unsharp 1.5 0.8" in l for l in lines)

    def test_wavelet_bspline(self):
        profile = _make_profile()
        profile.target.post.sharpening.method = "wavelet"
        profile.target.post.sharpening.wavelet_type = "bspline"
        profile.target.post.sharpening.layers = 3
        lines = _sharpening_lines(profile)
        assert any("wavelet 3 2" in l for l in lines)

    def test_wavelet_linear(self):
        profile = _make_profile()
        profile.target.post.sharpening.method = "wavelet"
        profile.target.post.sharpening.wavelet_type = "linear"
        profile.target.post.sharpening.layers = 2
        lines = _sharpening_lines(profile)
        assert any("wavelet 2 1" in l for l in lines)

    def test_sharpening_disabled(self):
        profile = _make_profile()
        profile.target.post.sharpening.enabled = False
        lines = _sharpening_lines(profile)
        assert lines == []


class TestExportLines:
    def test_tiff_deflate(self):
        profile = _make_profile()
        profile.target.post.export.format = "tiff"
        profile.target.post.export.deflate = True
        lines = _export_lines(profile, Path("/fake/output/final"))
        assert any("savetif" in l and "-deflate" in l for l in lines)

    def test_tiff_no_deflate(self):
        profile = _make_profile()
        profile.target.post.export.format = "tiff"
        profile.target.post.export.deflate = False
        lines = _export_lines(profile, Path("/fake/output/final"))
        assert any("savetif" in l for l in lines)
        assert not any("-deflate" in l for l in lines)

    def test_export_disabled(self):
        profile = _make_profile()
        profile.target.post.export.enabled = False
        lines = _export_lines(profile, Path("/fake/output/final"))
        assert lines == []


# ---------------------------------------------------------------------------
# Tests : pipeline.py — validation de session
# ---------------------------------------------------------------------------


class TestValidateSession:
    def test_missing_session_dir(self,tmp_path):
        from astro_pipeline.pipeline import validate_session, SessionError
        profile = _make_profile()
        with pytest.raises(SessionError):
            validate_session(tmp_path / "nonexistent", profile)

    def test_missing_folders(self, tmp_path):
        from astro_pipeline.pipeline import validate_session, SessionError
        profile = _make_profile(use_premade_masters=False)
        with pytest.raises(SessionError):
            validate_session(tmp_path, profile)