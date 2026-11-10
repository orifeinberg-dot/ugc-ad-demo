"""Offline tests. No keys, no internet, no money. Run with:  python3 -m pytest tests -q
They swap the web call for a pretend one, so the Claude / fal.ai / ElevenLabs code paths still get exercised."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import make_ads  # noqa: E402

SAMPLES = json.loads((make_ads.ROOT / "samples" / "sample_variations.json").read_text(encoding="utf-8"))["variations"]


def run_main(monkeypatch, tmp_path, *args, strict=None):
    real_load = make_ads.load_config

    def load(showcase=False):
        cfg = real_load(showcase)
        cfg["run"]["output_dir"] = str(tmp_path)      # write runs into the temp folder
        if strict is not None:
            cfg.setdefault("policy", {})["strict"] = strict
        return cfg

    monkeypatch.setattr(make_ads, "load_config", load)
    monkeypatch.setattr(sys, "argv", ["make_ads.py", *args])
    make_ads.main()
    return sorted(tmp_path.glob("run_*"))[-1]


def fake_services(policy_reply=None, policy_fails=False, eleven_ok=None, variations_reply=None):
    """A pretend internet. Returns (fake_http, calls). eleven_ok=N lets only the first N voice requests work."""
    calls = []
    state = {"eleven": 0}

    def fake_http(url, data=None, headers=None, method=None, timeout=120):
        calls.append(url)
        if "api.anthropic.com" in url:
            body = json.loads(data)
            if "exactly ONE thing" in body["messages"][0]["content"]:      # a request for new versions of one ad
                text = json.dumps(variations_reply or {"variations": [
                    dict(SAMPLES[1], hook=f"New opening {k}", changed="a new opening line") for k in (1, 2, 3)]})
            elif "creative strategist" in body["system"]:
                text = json.dumps({"variations": SAMPLES})
            else:
                if policy_fails:
                    raise RuntimeError("pretend network error")
                text = json.dumps(policy_reply or {"verdict": "pass", "issues": []})
            reply = {"content": [{"type": "text", "text": "```json\n" + text + "\n```"}],
                     "usage": {"input_tokens": 1000, "output_tokens": 500}}
            return json.dumps(reply).encode(), "application/json"
        if "queue.fal.run" in url and method is None:                       # submit a job
            model = url.split("queue.fal.run/")[1]
            body = json.loads(data)
            assert "prompt" in body or "audio_url" in body
            if "avatar" in model:                                             # the video model needs picture + voice
                assert body["image_url"].startswith(("http", "data:image/")) and body["audio_url"].startswith("data:audio/mpeg;base64,")
            return json.dumps({"status_url": f"https://queue.fal.run/{model}/status",
                               "response_url": f"https://queue.fal.run/{model}/result"}).encode(), "application/json"
        if url.endswith("/status"):
            return json.dumps({"status": "COMPLETED"}).encode(), "application/json"
        if url.endswith("/result"):
            if "avatar" in url:
                return json.dumps({"video": {"url": "https://cdn.example/clip.mp4"}}).encode(), "application/json"
            return json.dumps({"images": [{"url": "https://cdn.example/pic.png"}]}).encode(), "application/json"
        if "elevenlabs.io" in url:
            state["eleven"] += 1
            if eleven_ok is not None and state["eleven"] > eleven_ok:
                raise RuntimeError("HTTP 401 quota_exceeded (pretend)")
            assert json.loads(data)["model_id"]
            return b"ID3-fake-mp3", "audio/mpeg"
        if url.endswith(".png"):
            return b"\x89PNG-fake", "image/png"
        if url.endswith(".mp4"):
            return b"fake-mp4", "video/mp4"
        raise AssertionError(f"Unexpected call: {url}")

    return fake_http, calls


def test_dry_run_makes_everything_for_free_and_flags_the_risky_ad(monkeypatch, tmp_path):
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    run = run_main(monkeypatch, tmp_path, "--mode", "dry")
    assert calls == []                                              # no web calls at all
    for name in ("scripts.json", "policy_report.json", "prompts.json", "costs.json", "summary.html"):
        assert (run / name).exists()
    report = json.loads((run / "policy_report.json").read_text())
    assert [r["verdict"] for r in report] == ["pass", "pass", "pass", "review"]
    assert json.loads((run / "costs.json").read_text())["spent_usd"] == 0
    assert "AI-generated" in (run / "summary.html").read_text()


def test_full_run_with_pretend_services(monkeypatch, tmp_path):
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    for key in ("ANTHROPIC_API_KEY", "FAL_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    run = run_main(monkeypatch, tmp_path, "--mode", "full", "--video-for", "1")
    assert (run / "ad_1" / "image.png").exists() and (run / "ad_1" / "voice.mp3").exists()
    assert (run / "ad_1" / "video.mp4").exists()                    # asked for
    assert not (run / "ad_2" / "video.mp4").exists()                # not asked for
    report = json.loads((run / "policy_report.json").read_text())
    assert report[3]["verdict"] == "review"                         # local rules still catch it even if Claude says pass
    costs = json.loads((run / "costs.json").read_text())
    items = [line["item"] for line in costs["lines"]]
    assert any(i.startswith("Claude: scripts") for i in items) and any(i.startswith("Video, ad 1") for i in items)
    assert costs["spent_usd"] > 0.3
    assert sum("elevenlabs.io" in u for u in calls) == 4
    page = (run / "summary.html").read_text()
    assert 'preload="metadata"' in page and "playsinline" in page                  # phones do not download every video on load
    assert "Approve first, then make video" in page and f"--resume {run.name} --video-for 2" in page   # ad 1 has its video, ad 2 is next


def test_a_failed_claude_policy_check_is_never_a_pass(monkeypatch, tmp_path):
    fake, _ = fake_services(policy_fails=True)
    monkeypatch.setattr(make_ads, "http", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    run = run_main(monkeypatch, tmp_path, "--mode", "text")
    report = json.loads((run / "policy_report.json").read_text())
    assert all(r["verdict"] == "review" for r in report)
    assert report[0]["issues"][0]["category"] == "check-error"


def test_claude_flags_are_added_to_the_report(monkeypatch, tmp_path):
    reply = {"verdict": "review", "issues": [{"category": "personal-attributes", "quote": "your mom", "why": "Speaks to the viewer's family.", "fix": "Say a mom."}]}
    fake, _ = fake_services(policy_reply=reply)
    monkeypatch.setattr(make_ads, "http", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    run = run_main(monkeypatch, tmp_path, "--mode", "text")
    report = json.loads((run / "policy_report.json").read_text())
    assert report[0]["verdict"] == "review" and report[0]["issues"][0]["source"] == "claude"


TIP_ONLY = {"verdict": "pass", "issues": [{"severity": "tip", "category": "tone", "quote": "Calm, clear", "why": "Could sound flat.", "fix": "Add a smile."}]}


def test_tips_do_not_change_the_verdict_but_are_shown(monkeypatch, tmp_path):
    fake, _ = fake_services(policy_reply=TIP_ONLY)
    monkeypatch.setattr(make_ads, "http", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    run = run_main(monkeypatch, tmp_path, "--mode", "text")
    report = json.loads((run / "policy_report.json").read_text())
    assert [r["verdict"] for r in report] == ["pass", "pass", "pass", "review"]    # ad 4 still fails on the fixed rules
    assert report[0]["issues"][0]["severity"] == "tip"
    assert "Tip: tone" in (run / "summary.html").read_text()


def test_strict_setting_makes_tips_count(monkeypatch, tmp_path):
    fake, _ = fake_services(policy_reply=TIP_ONLY)
    monkeypatch.setattr(make_ads, "http", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    run = run_main(monkeypatch, tmp_path, "--mode", "text", strict=True)
    report = json.loads((run / "policy_report.json").read_text())
    assert all(r["verdict"] == "review" for r in report)


def test_policy_test_runs_the_checker_on_the_samples(monkeypatch, tmp_path, capsys):
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", ["make_ads.py", "--policy-test"])
    make_ads.main()
    out = capsys.readouterr().out
    assert "Ad 4: REVIEW" in out and "Ad 1: PASS" in out
    assert sum("api.anthropic.com" in u for u in calls) == 4        # one Claude check per sample ad


def test_voice_is_picked_by_gender_and_age_with_a_fallback():
    cfg = {"voice": {"ids": {"female": "F", "male": "M", "female_older": "FO", "male_young": ""}}}
    pick = lambda g, a: make_ads.pick_voice(cfg, {"voice": g, "voice_age": a})
    assert pick("female", "older") == "FO"          # the matching slot
    assert pick("female", "young") == "F"           # slot missing: fallback
    assert pick("male", "young") == "M"             # slot empty: fallback


def fal_image_calls(calls):
    return [u for u in calls if u.endswith("nano-banana-2")]


def fal_video_calls(calls):
    return [u for u in calls if u.endswith("/ai-avatar/v2/standard")]


def eleven_calls(calls):
    return [u for u in calls if "elevenlabs.io" in u]


def set_keys(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "FAL_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(key, "test-key")


def test_resume_makes_only_the_missing_voices(monkeypatch, tmp_path):
    set_keys(monkeypatch)
    fake, _ = fake_services(eleven_ok=1)                              # ElevenLabs runs out after the first voice
    monkeypatch.setattr(make_ads, "http", fake)
    run = run_main(monkeypatch, tmp_path, "--mode", "full")
    assert (run / "ad_1" / "voice.mp3").exists() and not (run / "ad_2" / "voice.mp3").exists()
    spent_first = json.loads((run / "costs.json").read_text())["spent_usd"]

    fake2, calls2 = fake_services()                                   # the problem is fixed
    monkeypatch.setattr(make_ads, "http", fake2)
    assert run_main(monkeypatch, tmp_path, "--resume") == run         # same folder, no new run
    assert all((run / f"ad_{i}" / "voice.mp3").exists() for i in range(1, 5))
    assert len(eleven_calls(calls2)) == 3                             # only the three that were missing
    assert not fal_image_calls(calls2) and not any("api.anthropic.com" in u for u in calls2)
    costs = json.loads((run / "costs.json").read_text())
    assert costs["spent_usd"] > spent_first                           # the new voices are added to the total
    assert any(line["item"].startswith("Claude: scripts") for line in costs["lines"])   # earlier spending is kept


def test_redo_voice_sets_the_old_video_aside_and_a_later_resume_rebuilds_it_from_the_saved_picture(monkeypatch, tmp_path):
    set_keys(monkeypatch)
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    run = run_main(monkeypatch, tmp_path, "--mode", "full", "--video-for", "1")
    assert (run / "ad_1" / "video.mp4").exists()

    calls.clear()
    run_main(monkeypatch, tmp_path, "--resume", "--redo", "voice")
    assert len(eleven_calls(calls)) == 4 and not fal_image_calls(calls)
    assert not (run / "ad_1" / "video.mp4").exists() and (run / "ad_1" / "video_previous.mp4").exists()

    calls.clear()
    run_main(monkeypatch, tmp_path, "--resume", "--video-for", "1")
    assert (run / "ad_1" / "video.mp4").exists()
    assert len(fal_video_calls(calls)) == 1                           # one new video
    assert not fal_image_calls(calls) and not eleven_calls(calls)     # picture and voice were reused from disk


def test_resume_keeps_a_finished_run_untouched(monkeypatch, tmp_path):
    set_keys(monkeypatch)
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    run = run_main(monkeypatch, tmp_path, "--mode", "full")
    calls.clear()
    run_main(monkeypatch, tmp_path, "--resume")
    assert calls == []                                                # nothing missing, nothing paid


def test_resume_rules_and_clear_messages(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["make_ads.py", "--resume", "--mode", "dry"])
    with pytest.raises(SystemExit, match="full mode"):
        make_ads.main()
    monkeypatch.setattr(sys, "argv", ["make_ads.py", "--redo", "voice"])
    with pytest.raises(SystemExit, match="--resume"):
        make_ads.main()
    with pytest.raises(RuntimeError, match="no earlier run"):
        run_main(monkeypatch, tmp_path, "--resume")                   # an empty runs folder


def test_voice_settings_are_sent_only_when_configured(monkeypatch):
    sent = []
    monkeypatch.setattr(make_ads, "http", lambda url, data=None, headers=None, **kw: (sent.append(json.loads(data)), (b"x", ""))[1])
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    make_ads.tts({"voice": {"model_id": "m"}}, "V", "hello")
    make_ads.tts({"voice": {"model_id": "m", "settings": {"stability": 0.4}}}, "V", "hello")
    assert "voice_settings" not in sent[0] and sent[1]["voice_settings"] == {"stability": 0.4}


def test_ads_that_share_a_voice_slot_take_turns():
    cfg = {"voice": {"ids": {"female": "F", "male": "M", "female_middle": ["A", "B"]}}}
    used = {}
    picks = [make_ads.pick_voice(cfg, {"voice": "female", "voice_age": "middle"}, used) for _ in range(3)]
    assert picks == ["A", "B", "A"]
    assert make_ads.pick_voice(cfg, {"voice": "male", "voice_age": "middle"}, used) == "M"


def test_the_voice_age_defaults_to_middle_and_is_cleaned():
    base = dict(SAMPLES[0])
    base.pop("voice_age", None)
    assert make_ads.clean_variation(base, 1)["voice_age"] == "middle"
    base["voice_age"] = "Older (60s)"
    assert make_ads.clean_variation(base, 1)["voice_age"] == "older"


def test_voice_test_makes_one_file_per_configured_voice(monkeypatch, tmp_path):
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    real_load = make_ads.load_config

    def load(showcase=False):
        cfg = real_load(showcase)
        cfg["run"]["output_dir"] = str(tmp_path)
        cfg["voice"]["ids"] = {"female": "F", "male": "M", "female_older": "OLDVOICE", "female_middle": ["A", "B"], "male_young": ""}
        return cfg

    monkeypatch.setattr(make_ads, "load_config", load)
    monkeypatch.setattr(sys, "argv", ["make_ads.py", "--voice-test"])
    make_ads.main()
    files = sorted(p.name for p in next(tmp_path.glob("voice_test_*")).iterdir())
    assert files == ["female.mp3", "female_middle_1.mp3", "female_middle_2.mp3", "female_older.mp3", "male.mp3"]
    assert sum("elevenlabs.io" in u for u in calls) == 5


def test_missing_key_gives_a_clear_message(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    with pytest.raises(RuntimeError, match="FAL_KEY"):
        make_ads.need_key("FAL_KEY")


def test_extract_json_handles_code_fences_and_extra_words():
    assert make_ads.extract_json('Sure! ```json\n{"a": 1}\n``` Hope it helps.') == {"a": 1}


def test_unreadable_json_from_claude_is_retried_once(monkeypatch, tmp_path):
    fake, _ = fake_services()
    state = {"script_calls": 0}

    def flaky(url, data=None, headers=None, method=None, timeout=120):
        if "api.anthropic.com" in url and "creative strategist" in json.loads(data)["system"]:
            state["script_calls"] += 1
            if state["script_calls"] == 1:        # first reply has an unescaped double quote inside a value
                bad = '{"variations": [{"hook": "She says "hi" to you", "x": 1}]}'
                reply = {"content": [{"type": "text", "text": bad}], "usage": {"input_tokens": 10, "output_tokens": 10}}
                return json.dumps(reply).encode(), "application/json"
        return fake(url, data, headers, method, timeout)

    monkeypatch.setattr(make_ads, "http", flaky)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    run = run_main(monkeypatch, tmp_path, "--mode", "text")
    assert state["script_calls"] == 2
    assert (run / "debug_scripts.txt").exists()                     # the unreadable reply is saved for debugging
    assert len(json.loads((run / "scripts.json").read_text())) == 4


def test_a_cut_off_claude_reply_gives_a_clear_message(monkeypatch, tmp_path):
    def cut(url, data=None, headers=None, method=None, timeout=120):
        reply = {"content": [{"type": "text", "text": '{"variations": ['}], "stop_reason": "max_tokens",
                 "usage": {"input_tokens": 10, "output_tokens": 4000}}
        return json.dumps(reply).encode(), "application/json"

    monkeypatch.setattr(make_ads, "http", cut)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="cut off"):
        run_main(monkeypatch, tmp_path, "--mode", "text")


def test_variations_in_a_dry_run_cost_nothing_and_show_what_changed(monkeypatch, tmp_path):
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    base = run_main(monkeypatch, tmp_path, "--mode", "dry")
    run = run_main(monkeypatch, tmp_path, "--mode", "dry", "--variations-of", "2", "--change", "a new opening line")
    assert run != base and run.name.endswith("_ad2_variations")
    assert calls == []
    assert len(json.loads((run / "scripts.json").read_text())) == 3          # three versions by default
    origin = json.loads((run / "variation_of.json").read_text())
    assert origin["base_ad"] == 2 and origin["change"] == "a new opening line" and origin["from_run"] == base.name
    page = (run / "summary.html").read_text()
    assert "<th>What changed</th>" in page                                       # the overview table shows what differs
    assert f'href="../{base.name}/summary.html"' in page                      # a link back to the original results page
    assert "Only one thing changed: a new opening line" in page and "What changed:" in page and "AI-generated" in page


def test_variations_keep_the_same_person_and_voice_and_pay_for_a_new_picture_only_when_the_person_changes(monkeypatch, tmp_path):
    set_keys(monkeypatch)
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    base = run_main(monkeypatch, tmp_path, "--mode", "full")
    base_voice_call = eleven_calls(calls)[1]                                  # the voice used for ad 2
    assert (base / "ad_2" / "voice_id.txt").read_text() in base_voice_call

    same = dict(SAMPLES[1], hook="A new opening", changed="a new opening line")
    other = dict(SAMPLES[1], hook="A new opening", persona_look="a man in his 60s with a grey beard", changed="a different person")
    fake2, calls2 = fake_services(variations_reply={"variations": [same, other]})
    monkeypatch.setattr(make_ads, "http", fake2)
    run = run_main(monkeypatch, tmp_path, "--mode", "full", "--variations-of", "2", "--change", "who is speaking", "-n", "2")
    assert len(fal_image_calls(calls2)) == 1                                  # only the new person needs a new picture
    assert (run / "ad_1" / "image.png").read_bytes() == (base / "ad_2" / "image.png").read_bytes()   # reused, not paid for
    assert (run / "ad_1" / "voice_id.txt").read_text() == (base / "ad_2" / "voice_id.txt").read_text()   # same voice
    assert len(eleven_calls(calls2)) == 2
    costs = json.loads((run / "costs.json").read_text())
    assert [l["item"] for l in costs["lines"] if l["item"].startswith("Image")] == ["Image, ad 2"]
    assert make_ads.json.loads((run / "scripts.json").read_text())[0]["hook"] == "A new opening"
    run_main(monkeypatch, tmp_path, "--resume", run.name)                    # a later resume keeps the "versions of" note
    assert "Only one thing changed" in (run / "summary.html").read_text()


def test_variation_rules_and_clear_messages(monkeypatch, tmp_path):
    fake, _ = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    run_main(monkeypatch, tmp_path, "--mode", "dry")
    for argv, message in ((["--variations-of", "1"], "one thing to change"),
                          (["--change", "x"], "--variations-of"),
                          (["--resume", "--variations-of", "1", "--change", "x"], "new run")):
        monkeypatch.setattr(sys, "argv", ["make_ads.py", *argv])
        with pytest.raises(SystemExit, match=message):
            make_ads.main()
    with pytest.raises(RuntimeError, match="Ad 9 is not in"):
        run_main(monkeypatch, tmp_path, "--mode", "dry", "--variations-of", "9", "--change", "x")


def test_keep_person_forces_the_original_person_even_if_claude_rewrites_it(monkeypatch, tmp_path):
    set_keys(monkeypatch)
    fake, calls = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    base = run_main(monkeypatch, tmp_path, "--mode", "full")
    drift = dict(SAMPLES[1], persona_look="a man in his 30s, slightly different wording", hook="Fresh opening", changed="a new opening line")
    fake2, calls2 = fake_services(variations_reply={"variations": [drift, drift]})
    monkeypatch.setattr(make_ads, "http", fake2)
    run = run_main(monkeypatch, tmp_path, "--mode", "full", "--variations-of", "2", "--change", "x", "-n", "2", "--keep-person")
    assert not fal_image_calls(calls2)                                        # no new picture was paid for
    assert json.loads((run / "scripts.json").read_text())[0]["persona_look"] == SAMPLES[1]["persona_look"]
    assert (run / "ad_2" / "voice_id.txt").read_text() == (base / "ad_2" / "voice_id.txt").read_text()
    monkeypatch.setattr(sys, "argv", ["make_ads.py", "--keep-person"])
    with pytest.raises(SystemExit, match="--variations-of"):
        make_ads.main()


def test_a_rebuilt_page_keeps_the_estimate_and_explains_a_higher_total(monkeypatch, tmp_path):
    set_keys(monkeypatch)
    fake, _ = fake_services()
    monkeypatch.setattr(make_ads, "http", fake)
    run = run_main(monkeypatch, tmp_path, "--mode", "full", "--video-for", "1", "2")
    first = json.loads((run / "costs.json").read_text())
    assert "spent in this run" in (run / "summary.html").read_text()

    run_main(monkeypatch, tmp_path, "--resume")                               # no --video-for this time
    again = json.loads((run / "costs.json").read_text())
    assert again["full_run_estimate"]["total_usd"] == first["full_run_estimate"]["total_usd"]   # videos already made still count

    again["lines"].append({"item": "Test video", "detail": "an earlier attempt", "usd": 5.0})
    (run / "costs.json").write_text(json.dumps(again), encoding="utf-8")
    run_main(monkeypatch, tmp_path, "--resume")
    assert "spent in total, including test attempts and redone parts" in (run / "summary.html").read_text()
