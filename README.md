# UGC Ad Demo

Turn one short product brief into several UGC-style ad variations: scripts, a policy check, pictures, a voice track, and an optional talking video. Everything lands in one folder per run, with a summary page you can open and show.

The example product, **SafeNest**, is made up. Every person, voice and video this tool makes is AI-generated and is labeled that way.

## What happens in one run

1. **Scripts.** Claude Sonnet 5.5 reads `brief.md` and writes 3 to 5 variations. Each one has a different hook type, persona and format, with spoken lines, scenes, on-screen text and a call to action.
2. **Policy check.** Fixed rules catch obvious problems for free (fear claims, guarantees, before-and-after, fake testimonials). Claude Haiku 5.5 then reads each script for the sneakier ones. Every ad is marked **pass** or **review**, with the reason.
3. **Pictures, voice, video.** One picture per ad (fal.ai), a voice track (ElevenLabs), and, only if you ask, a talking video (fal.ai avatar model).
4. **Summary.** `summary.html` shows every ad, its check result, and the cost in US dollars.

## Quick start (Windows with WSL, Mac or Linux)

Needs Python 3.11 or newer. Nothing to install: the script uses only Python's built-in libraries.

```bash
python3 --version              # 3.11 or higher
python3 make_ads.py --open     # free dry run: no keys, no cost, never breaks
```

Open `runs/<latest>/summary.html` if it does not open by itself.

## The three modes

| Mode | What runs | Keys needed | Cost (4 ads) |
|---|---|---|---|
| `dry` (default) | Sample scripts, placeholder pictures, silent audio | none | $0 |
| `text` | Claude writes and checks the scripts | Anthropic | about $0.04 |
| `full` | Plus real pictures and voice | Anthropic, fal.ai, ElevenLabs | about $0.45 |

```bash
cp .env.example .env                                    # then paste your keys into .env
python3 make_ads.py --mode text                         # Claude only
python3 make_ads.py --mode full                         # pictures + voice
python3 make_ads.py --mode full --video-for 2           # real video for ad 2 only
python3 make_ads.py --mode full --showcase --video-for 1 2 3 4   # best quality, video for all
```

Video takes 1 to 3 minutes per ad. It is off by default, so a normal run never makes one by accident.

## Approve first, then make video

Video is the expensive part, so it comes last. A normal run makes the script, the policy check, a picture and a voice for every ad. Read the lines, listen to the voices, and choose. Then turn only the ads you approve into videos:

```bash
python3 make_ads.py --resume --video-for 2          # video for ad 2 of the latest run (about $0.95)
python3 make_ads.py --resume run_20261110_175425_ad2_variations --video-for 1 3
```

The picture and voice already made are reused, so you pay only for the video. The summary page shows this command for the run you are looking at.

## When part of a run fails: resume it

If one piece fails (for example ElevenLabs runs out of credits), the run still finishes and the ad shows a placeholder. After you fix the cause, finish that run without paying for the good parts again:

```bash
python3 make_ads.py --resume                    # the latest run: makes only what is missing
python3 make_ads.py --resume run_20261110_072918   # a specific run folder
python3 make_ads.py --resume --video-for 2      # also make a video for ad 2
python3 make_ads.py --resume --redo voice       # make every voice again (after changing voices)
python3 make_ads.py --resume --redo image voice video
```

Scripts, the policy check and every real file already made are kept. The cost of each resume is added to the run's total. If you redo a picture or a voice, the old video no longer matches, so it is saved as `video_previous.mp4`. Make a new one with `--video-for`.

## Make new versions of an ad that works

Pick one ad from a finished run and say the ONE thing to change. Claude writes new versions that keep everything else the same, so if a version wins you know why.

```bash
python3 make_ads.py --variations-of 2 --change "a different opening line"
python3 make_ads.py --variations-of 2 --change "end with a question instead of an offer" -n 5 --mode full
python3 make_ads.py --variations-of 1 --change "a retired man instead of a mom" --from-run run_20261110_074403
```

- `--variations-of` is the ad number. `--change` is the one thing, in plain words. `-n` is how many versions (1 to 5, default 3). `--from-run` is the run to start from (default: the latest).
- The new versions go in a new folder ending in `_adN_variations`. Your original run is not touched.
- If the person is the same, the tool reuses the original picture and the same voice, so you only pay for the new voice (and the video, if you ask). A new person gets a new picture.
- Add `--keep-person` to always keep the original person, picture and voice, even if Claude rewrites the description. Use it when your change is about the words, not the person.
- The new versions are stills with a voice, like any run. Approve the ones you like, then add video with `--resume <that run> --video-for N` (see "Approve first, then make video").
- The new versions go through the same policy check. The summary page says what changed in each one.
- It does not decide which ad "works". You choose the ad, based on real results from a test campaign.

## Settings: `config.toml`

Every model name and price is a setting. To swap a model, edit one line:

- `claude.script_model`, `claude.policy_model`: the Claude models
- `image.model`: any fal.ai image model ID
- `video.model`: any fal.ai talking-avatar model ID (it takes a picture and a voice)
- `voice.ids`, `voice.settings`: your ElevenLabs voices and how natural they sound (see "Choosing voices")
- `prices.*`, `image.usd_per_image`, `video.usd_per_second`: used for the cost estimate

Prices change often. Check each provider's pricing page now and then.

## Rough cost per ad (US dollars)

| Piece | About |
|---|---|
| Picture | $0.08 |
| Voice | $0.02 (rough; ElevenLabs bills through plan credits) |
| Video, standard avatar model | $0.95 for 17 seconds |
| Video, pro avatar model (`--showcase`) | $1.90 for 17 seconds |
| Claude, scripts + checks for a whole run | about $0.04 |

## Choosing voices

Each ad has a person with a gender and an age group (young, middle, older). The ad uses the voice you set for that group in `config.toml` under `[voice.ids]`, for example `female_older`. A slot left as `""` uses the plain `female` or `male` voice.

1. In ElevenLabs, open Voices, then Explore. Filter by gender, age and a conversational style. Copy the Voice ID.
2. Paste the IDs into `config.toml`.
3. Listen first, for about a cent per voice: `python3 make_ads.py --voice-test` makes one short file per voice in `runs/voice_test_.../`.

To sound less like a narrator, `[voice.settings]` uses lower stability (more natural ups and downs) and a slightly faster speed. You can also try `model_id = "eleven_v3"` with `stability = 0.5`. Note that some ElevenLabs plans only allow premade voices through the API.

## What is in a run folder

```
runs/run_YYYYMMDD_HHMMSS/
  summary.html        open this
  scripts.json        every script
  prompts.json        the picture, video and voice prompts used
  policy_report.json  pass / review and the reasons
  costs.json          what was spent, plus the estimate for a full run
  brief.md            the brief this run used
  ad_1/ ad_2/ ...     image, voice, video
```

## About the policy check

It finds obvious problems fast. **A pass is not approval from Meta.** A person should review every ad before it runs. If the Claude check fails for any reason (network, bad reply), the ad is marked **review**, never pass.

## Trust and labeling

- Every picture, voice and video is labeled AI-generated on the summary page.
- Scripts are written for a fictional character. They must not claim to be a real customer or expert.
- Do not publish AI people as real testimonials.

## Optional: Midjourney for style references

Midjourney has no official API, so this tool does not call it. If you like it for looks, make a few style references by hand, describe the look in words, and paste that into `persona_look` and `setting` for your variations.

## Tests

```bash
python3 make_ads.py --check          # quick check of settings and policy rules
pip install pytest && python3 -m pytest tests -q   # offline tests, no keys, no cost
```

## Where this could go

1. **Auto-test the variations.** Upload each approved ad to a small test campaign with equal budget, then let a script read the results and pause the losers.
2. **Pull in performance data.** Read cost per result by ad from the Meta Marketing API into one table that sits beside each script, so you can see which hook types and personas win.
3. **Pick winners automatically.** `--variations-of` already makes new versions of an ad you choose. The next step is to let the performance data choose the winner and start that run for you.
