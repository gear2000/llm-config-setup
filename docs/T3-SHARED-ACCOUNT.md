# T3 Code for several people on one server account

[T3 Code](https://github.com/pingdotgg/t3code) lets people run coding agents (Claude Code, Codex) from a phone, a browser, or a desktop app. `tools/t3-shared-account.sh` runs one always-on T3 server per person under a single Unix account:

- Everyone shares that account's agent logins.
- Each person has their own GitHub token, git identity, data folder and worktrees.
- Each person signs in to the T3 app with their own T3 account. Nobody needs SSH.

```
Setup
│
├── ADMIN, once
│   ├── sudo useradd -m -s /bin/bash llm             (any account name works)
│   └── sudo loginctl enable-linger llm              (servers start at boot, survive logout)
│
├── ADMIN, as the account  (ssh llm@host  or  sudo -iu llm)
│   ├── curl -fsSL https://t3.codes/install.sh | sh
│   ├── claude auth login   and/or   codex login     (shared by every user)
│   └── T3_USERS="alice:3773 bob:3774" bash tools/t3-shared-account.sh
│       ├── asks each user's GitHub token, git name and email
│       ├── t3-alice.service   port 3773, data ~/alice/t3_data, projects ~/alice/
│       ├── t3-bob.service     port 3774, data ~/bob/t3_data,   projects ~/bob/
│       ├── git push over HTTPS uses each server's own token
│       ├── new threads start in a git worktree
│       └── checks ports, data folders, tokens
│
├── ADMIN, once per user
│   └── bash tools/t3-shared-account.sh connect alice
│       └── prints a link + short code → send both to Alice
│
├── ALICE (no SSH, no terminal)
│   ├── open the link → sign in or create a T3 account → approve
│   ├── install the T3 Code app (phone or desktop), or open https://app.t3.codes
│   └── sign in with the same account → pick her server
│
└── ADMIN, upkeep
    ├── check:          bash tools/t3-shared-account.sh verify
    ├── add a user:     T3_USERS="alice:3773 bob:3774 carol:3775" bash tools/t3-shared-account.sh
    ├── after t3 update: systemctl --user restart t3-alice t3-bob
    └── never run as the account: t3 · t3 up · bare t3 connect · t3 service install · t3 uninstall
```

## Rules

- Clone repositories over HTTPS. SSH remotes push with the account's SSH key, not the user's token.
- The script owns `credential.https://github.com.helper` in the account's `~/.gitconfig`. A person pushing by hand from a shell must `export GH_TOKEN=…` first.
- Other `t3` commands need the user's data folder, for example `t3 doctor --base-dir ~/alice/t3_data`.

## Limits

- One Unix account means no wall between users. Each user's agent can read the other users' tokens and repositories. Give each user a fine-grained GitHub token that reaches only the repositories they need.
- All users share one Claude/Codex subscription and its usage limits. Check that your plan allows that.
