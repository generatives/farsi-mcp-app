# Curriculum

These YAML files are the curriculum the practice server teaches from. The server
loads them into memory at startup and refuses to start if they're invalid, so
edit them through pull requests. The full plan and the reasoning behind it are in
the [curriculum doc](https://claude.ai/artifact/JU468uZsGBx4cvPJ8UHE4P).

This is a **reduced** curriculum, enough for a first 30 minutes or so:

| File | Contents |
| --- | --- |
| `manifest.yaml` | Version label, notes, and the list of allowed topic tags |
| `grammar.yaml` | The five Stage 0 grammar patterns, G1 to G5 |
| `functions.yaml` | The five functions that need only Stage 0 grammar, F1 to F5 |
| `phrases.yaml` | Set phrases for F1 to F4 |
| `words/tier1.yaml` | About 50 everyday nouns, adjectives, adverbs and pronouns |

Ids are unique across all files and start with `g.`, `f.`, `p.` or `w.`. Your
learning history points at these ids, so don't rename one once you've practised it.

The Persian is colloquial Tehrani and still needs a native speaker's check.
