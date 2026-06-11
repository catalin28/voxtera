# Website audio sample — hotel concierge call script

A 6-turn "first evening at the hotel" scenario, developed live against the
real concierge (hotel mode, `hotel_id=demo`, session `website-sample-1` on
2026-06-10). Every BOT line below is what the pipeline actually answered —
expect close (not identical) wording on the real call, since the render is
generated each time.

## How to record

1. `scripts/demo-mode.sh hotel` (and deploy `main` first if not done).
2. Call the WhatsApp number; let the greeting finish.
3. Read the GUEST lines naturally, pausing after each answer.
4. Afterwards, pull `logs/calls/<session_id>/recording.wav` from the droplet
   (stereo: caller left, bot right) — newest folder under `logs/calls/`.

## Script

**BOT (greeting):** Hello! You've reached the concierge at Grand Hôtel
Lumière. How can I help you today?

**GUEST:** Hi! I just checked in an hour ago. What time is breakfast
tomorrow morning?

**BOT:** Good evening! Le Mirador, our rooftop restaurant, serves breakfast
starting at six in the morning, and room service is available from six as
well if you'd prefer to breakfast in your room — both run through eleven
o'clock. Shall I have someone bring you the breakfast menu, or would you
like me to walk you through ordering from your room?

**GUEST:** The rooftop sounds lovely, I'll go up myself. Actually — I've had
a long flight. Do you have a spa where I could book a massage?

**BOT:** Absolutely — Spa Lumière is on the lower-ground floor, and we have
massage treatments available: Swedish Relaxation runs sixty or ninety
minutes at a hundred thirty and a hundred seventy-five euros respectively.
Since you've just arrived, shall I ring down to the spa desk and have them
hold a slot for you this evening, or would you prefer to stop by tomorrow
morning before breakfast?

**GUEST:** This evening would be perfect — the ninety-minute one. How late
is the spa open tonight?

**BOT:** The spa closes at nine o'clock this evening, so a ninety-minute
treatment would need to start by seven-thirty at the latest. Come down to
Spa Lumière whenever you're ready — just plan to arrive fifteen minutes
early for your consultation, and we'll have you settled in straightaway.

*(Nice moment: it computes the latest start time from the closing hour.)*

**GUEST:** Wonderful. And after the massage — where should I have dinner?
Something special, it's my first night.

**BOT:** You're staying at the Grand Hôtel Lumière, and you have two
wonderful options right here. Le Mirador, our rooftop restaurant where
you'll breakfast tomorrow, serves dinner as well — it's the perfect choice
for a first evening, with that view and the energy of the room. …

**GUEST:** Le Mirador it is then — I'll mention it at the desk on my way to
the spa. Thank you so much, you've been wonderful!

**BOT:** You're very welcome — enjoy your massage, and do ask for a table by
the window if you can when you check in at Le Mirador; the city lights at
night are really something. A lovely first evening ahead.

## Caveats found while scripting

- **Avoid asking the bot to MAKE a reservation** ("can you book…", "make a
  reservation for me") — booking is an action the concierge can't perform
  yet, so the escalation classifier hands off to "a colleague". Phrase the
  close as the guest handling it ("I'll mention it at the desk"). Action
  support is the remaining P1.4 work (port create_ticket etc. into the
  concierge).
- Keep guest turns to one or two sentences — matches the spoken brief render
  and keeps the sample tight (~90 seconds total).
