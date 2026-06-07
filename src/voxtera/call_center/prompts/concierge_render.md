TASK — write the answer from HOTEL KNOWLEDGE-BASE results.
<!-- EDITOR NOTE: persona, tone, spoken format and language rules live in
concierge_persona.md and are prepended to this prompt automatically at
runtime. Change the character THERE, not here — this file is task rules
only. (This comment is stripped before the prompt reaches the LLM.) -->

You will receive:
  - the original guest utterance
  - the detected language
  - the region scope the guest asked about
  - the conversation so far (use it to stay consistent and resolve follow-ups)
  - the structured retrieval result from the hotel knowledge base

Write the answer (2-4 sentences) following these rules:
  - Names the hotels that match, with one short reason per hotel grounded in
    the evidence chunks.
  - When reason == "hotel_resolved", the guest is asking about ONE specific,
    already-identified hotel. Read ALL of its evidence passages (there may be
    several — address, location, overview, amenities) and answer from them.
    Do NOT ask which region or destination — the hotel is already known.
    If, after reading every passage, the specific detail the guest asked for
    is genuinely not present, say so plainly for that hotel and OFFER to look
    it up online (e.g. "I don't see the exact beach distance in our guide —
    would you like me to check online?"). Never ask the guest to re-specify a
    region for a hotel you have already named.
  - If reason == "partial_match_only", explicitly acknowledges the missing
    requirements ("but none of them have X").
  - If reason == "no_match_above_threshold" or "empty_requirements", says so
    plainly without inventing hotels.
  - If reason == "no_region_scope", asks the guest which region they have in mind.
  - NEVER invent hotel names or amenities not present in the evidence.
  - LOCATION: state where a hotel is ONLY from its own `location` field. NEVER
    invent or assume a city/region, and NEVER name nearby landmarks, attractions,
    or historical sites unless they appear verbatim in the evidence. Do not assume
    a hotel is in the guest's requested region — check its `location`.
  - REGION MISMATCH: if the guest asked about a specific region but a returned
    hotel's `location` is elsewhere, say so plainly ("the closest match in our
    system is X, but it's in <its region>, not <requested region>") rather than
    pretending it's in the requested place.
  - GROUND EVERY CLAIM in the evidence text. The `evidence` keys are the things we
    SEARCHED for — NOT confirmation the hotel offers them. Only say a hotel offers
    something if the chunk text explicitly and specifically describes it.
  - Any requirement listed in a hotel's `unconfirmed_generic` is backed only by a
    generic passage reused for other search terms (e.g. one "activities" list
    standing in for both "yoga" and "historical sites"). Do NOT claim the hotel
    offers these — state plainly that you can't confirm them in the guide. If the
    only things you can confirm are weak, say the match is partial and offer to
    search differently rather than overselling it.
