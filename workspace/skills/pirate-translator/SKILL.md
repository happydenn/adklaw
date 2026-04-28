---
name: pirate-translator
description: Translates English text into 1700s pirate dialect. Use this skill when the user asks to "translate to pirate", "make this sound like a pirate", or otherwise indicates they want pirate-style rewriting.
---

# Pirate Translator

When invoked, rewrite the user-provided English text in 1700s pirate dialect
following these rules:

1. Replace common words:
   - "you" → "ye"
   - "are" → "be"
   - "my" → "me"
   - "yes" → "aye"
   - "friend" → "matey"
   - "treasure" → "booty"
2. Open with an exclamation like "Arrr!" or "Avast!".
3. Sprinkle in nautical imagery (ships, sails, the briny deep) where it
   fits naturally — but don't force it on every line.
4. Keep the original meaning intact. Do not invent facts.
5. Keep it to roughly the same length as the input.

Return only the translated text — no preamble, no explanation.
