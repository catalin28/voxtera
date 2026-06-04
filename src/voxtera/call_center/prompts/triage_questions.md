# Triage Clarification Questions

Localised, voice-channel-friendly clarification prompts used by the
Triage layer when the caller's utterance lacks a critical context
slot. Format: one `## <locale>` section per language; inside it,
`- <slot>: <question>` lines. The locale code is ISO-639-1. The slot
names must match the constants in `triage.py` (`geography`,
`hotel_or_recommend`, `non_negotiable`).

Add a new locale by adding another `## <locale>` section with the
same three slot lines. Keep prompts terse — every word of bot output
silences the caller's mic for ~330 ms during TTS playback.

## tr
- geography: Nereye gitmek istiyorsunuz?
- hotel_or_recommend: Belirli bir otel mi arıyorsunuz, yoksa öneri mi istersiniz?
- non_negotiable: Mutlaka olması gereken bir şey var mı (helal yemek, erişilebilirlik gibi)?

## en
- geography: Which destination are you thinking of?
- hotel_or_recommend: Are you asking about a specific hotel, or looking for suggestions?
- non_negotiable: Is there anything that must be available (halal food, accessibility, etc.)?

## es
- geography: ¿A qué destino está pensando ir?
- hotel_or_recommend: ¿Pregunta por un hotel en concreto o busca recomendaciones?
- non_negotiable: ¿Hay algo imprescindible (comida halal, accesibilidad...)?
