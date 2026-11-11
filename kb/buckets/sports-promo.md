# sports-promo

_Built from 3 of your edits; numbers from kb/profile/sports-promo.json, selection from kb/keeper/sports-promo.json, rules from kb/notes.md. Rebuild with `videtor kb bucket sports-promo`; write what the numbers cannot carry in kb/notes.md as `- [sports-promo] ...`._

## Numbers

- frame [1080, 1920], 35.355 cuts a minute; shots median 1.21 s, 10% under 0.533 s, 90% under 2.836 s; 7% speed-changed

## Rules

- [sports-promo] Three kinds: the pre-match hype reel (30-48 s, fastest: median shot 0.36 s), the practice-day wrap (23-28 s, median 0.58 s) and the match-day reel (about 57 s, a stadium build-up into the players).
- [sports-promo] Pace: an opening hold of about 2 s (an aerial or the empty stadium, floodlights, or a tight detail such as an eye or gloves), then held shots of about 0.5 s, flicker bursts of 6-10 sub-shots of 1-3 frames on one player (about one a minute), and a 1-2.5 s hold to close.
- [sports-promo] Cuts follow the music's accents (hits, snares, whooshes), not a strict beat grid: half of them sit on a strong onset; use "cut_on": "accents".
- [sports-promo] Between shots: mostly hard cuts; a 3-frame white exposure flash on about one cut in six, a 3-frame grey film-scan static on about one in fifteen (more at section changes), now and then a whip or a spin with motion blur.
- [sports-promo] Looks: short black-and-white runs of 2-3 shots (about 10% of the runtime), sometimes a colour shot turning black and white; a warm, saturated grade with deep blues (teal and gold), soft vignette; fisheye and vignette on stadium establishers.
- [sports-promo] Moves: about a quarter of the shots push in, a tenth pull out; low angles, wide lenses, Dutch tilts, golden-hour flares.
- [sports-promo] Content: players walking to camera, portraits looking into the lens, batting and bowling in the nets, warm-ups, gear (helmets, gloves, pads, bat, ball, spikes on the grass) in close-up; close on the team logo (often a helmet badge), floodlights or a walk-off.
- [sports-promo] Text: one short uppercase line per section, small, bottom centre ("THE FOCUS.", "LOCKED IN.", "FIRED UP."); silhouette and title animations are made outside the edit.

## From your renders (8)

- 83.7 cuts a minute; shots median 0.4 s, 10% under 0.04 s, 90% under 1.644 s; first shot 2.42 s, last 1.24 s.
- Held shots (outside flicker bursts) median 0.5 s; 1.525 flicker bursts a minute of 8.0 sub-shots (1-3 frames each).
- Pace through the runtime (median shot by tenth): 1.52, 0.56, 0.76, 0.52, 0.61, 0.43, 0.36, 0.56, 0.89, 0.56.
- Between shots: cut 66%, flash 14%, static 7%, overlay 4%, whip 3%, black 3%, spin 3% (transition frames median 3.0).
- Black and white: 10% of the runtime in 5.53 runs a minute; 0.82 shots a minute turn black and white.
- Moves per shot: push-in 28%, pull-out 9%, spin 8%, shake 5%.
- Grade: saturation 122.375, contrast 42.85, warmth (R-B) 1.5, corners/centre 0.915.
- Music: 129.2 BPM median; cuts on a beat 28% (chance 26%), on a half beat 48%: cuts follow the accents, not a strict grid (the sound at a cut is 1.23x as loud in onsets as elsewhere; 34% of the 82.8 hits a minute land on a cut).
- Shows: walk 17%, portrait 16%, batting 15%, bowling 9%, nets 8%, helmet 7%, graphic 6%, fielding 4%, floodlights 3%, studio 3%.
- Opens on gear, stadium, aerial, batting, gear, arrival, portrait, stadium; closes on walk, graphic, nets, graphic, graphic, nets, logo, graphic.

- **hype** (4, 39.72 s): 92.4 cuts a minute, median shot 0.36 s, black and white 10%, transitions cut 68%, flash 13%, static 7%, spin 4%.
- **matchday** (1, 56.72 s): 83.6 cuts a minute, median shot 0.42 s, black and white 0%, transitions cut 75%, black 10%, whip 6%, flash 4%.
- **reel** (1, 27.68 s): 93.2 cuts a minute, median shot 0.3 s, black and white 14%, transitions cut 49%, flash 30%, static 21%.
- **wrap** (2, 25.44 s): 71.95 cuts a minute, median shot 0.58 s, black and white 8%, transitions cut 62%, flash 18%, overlay 12%, static 5%.

## Learnt policy (videtor kb train)

- From 8 reels: held shot 0.50 s, open 2.05 s, close 1.04 s; 1.2 flicker bursts a minute; 51% of cuts on an accent within 0.26 s.
- Effect rates (plan/fx.py): flash 0.17, static 0.07, whip 0.03, spin 0.04, push-in 0.23, pull-out 0.07, shake 0.04, bw_runs_per_min 2.95, bw_run_shots 2.00.
- Held out in turn, its cuts scored -8.01 against the reel (0 = the reel's own style; another real reel scores about -8), the reels' medians -8.46, the old XML profile -13.25.

## Your edits

## Gujarat Giants.xml

254.2 s, 108 shots from 18 takes; median shot 1.21 s (10% under 0.42 s, 90% under 2.67 s); 1% of shots speed-changed.
Music, a medley: Ariel Shalom - Star Walk from 0.0 s at 3.3-58.1 s (41 shots, median 1.25 s); Ariel Shalom - Star Walk from 0.0 s at 145.7-200.4 s (44 shots, median 1.06 s); AlexGrohl - Breath of Light from 0.0 s at 226.7-245.1 s (6 shots, median 1.79 s).
Pauses: silence 58.1-145.7 s, silence 200.4-226.7 s, black 58.1-86.4 s, black 101.9-142.3 s, black 200.4-226.7 s.
Ramps: 100% -> 50% (x2).
Layers: 4 stacked layers; 6 still photos; adjustment layers: Flash blur x16.
SFX: other x23, whoosh x2.

## TPL.xml

28.1 s, 18 shots from 14 takes; median shot 1.02 s (10% under 0.53 s, 90% under 2.7 s); 6% of shots speed-changed.
Music: Nor Kin4life - On the Dash - No Lead Voc from 10.6 s at 0.0-28.1 s (18 shots, median 1.02 s).

## leander.xml

28.1 s, 18 shots from 14 takes; median shot 1.02 s (10% under 0.53 s, 90% under 2.7 s); 6% of shots speed-changed.
Music: Nor Kin4life - On the Dash - No Lead Voc from 10.6 s at 0.0-28.1 s (18 shots, median 1.02 s).
