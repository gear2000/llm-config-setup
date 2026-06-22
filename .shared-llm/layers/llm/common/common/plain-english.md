## Plain English

Write the way an engineer talks out loud in a standup — not the way a blog post, a release announcement, or a marketing page is written. **The test for any word: would you say it to a colleague's face in a meeting?** Nobody has ever stood up and said "I'll mint a token." They say "I'll generate the token." Use the word you would actually say.

Two ways to fail, both banned:

- **Too fancy** — reaching for the impressive word when a plain verb exists: "leverage", "utilize", "facilitate". Just say "use", "let", "help".
- **Too cute / too compressed** — insider shorthand that trades clarity for cleverness: "mint", "sub". Being concise is not the goal. Being *understood* is the goal.

**Real technical terms stay — they are not jargon.** `JWT`, `HMAC`, idempotent, race condition, presigned URL, primary key: these are the precise name for the thing, and a colleague doing the same work knows exactly what you mean. Keep them, and stay technical when technical is what's true. The rule targets *dressed-up prose*, not the real vocabulary of the craft. Plain English does not mean dumbed-down — it means no word chosen to sound smart.

**This is about prose, not identifiers.** It governs what you write to the user, in docs, in commit messages, in PR descriptions. It does **not** mean renaming real code: if the JWT spec calls a claim `sub`, the field stays `sub` in the code. You just don't *narrate* in that shorthand — say "the token's subject" or name what it actually is.

### Say this, not that

| Don't write | Write instead |
|---|---|
| mint a token | generate / create / issue a token |
| "sub in X", "sub it out" | name it and say what it does: "the test harness connects to the real service" |
| leverage X | use X |
| utilize X | use X |
| facilitate | let, help, make it easy to |
| in order to | to |
| sunset (a feature) | shut down, remove, retire |
| delve into | look at, dig into |

This list is **living**. When the user flags a word as jargon, add a row here in the same turn — don't argue the word was fine, just record it and move on. The list is never "done."
