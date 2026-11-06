# Set up your own T3 Code server

[T3 Code](https://github.com/pingdotgg/t3code) runs coding agents (Claude Code, Codex) on a server. You control them from the T3 Code phone app, the desktop app, or <https://app.t3.codes>. This guide gives one person their own always-on T3 server under their own Unix login, reachable from their phone through T3 Connect.

Flow overview: [t3-setup.html](t3-setup.html), a one-page picture you can open in a browser.

**Using an agent?** Give your agent (Claude Code, Codex, …) this request:

> Read docs/T3-SETUP.md in the llm-config-setup repository and set up my T3 Code server. Stop and tell me whenever I need to log in or approve something.

## Who does what

- **Admin:** someone with `sudo` on the server. Needed for step 1 only.
- **You:** the person who will use T3. Steps 2 to 5 run as **your own Unix login** on the server, never as root. An admin can do them for you with `sudo -iu <your login>`, but every command still runs as you.
- **Your phone:** step 6.
- **Your agent**, if you use one, does steps 2 to 5. It stops at the two **human steps** (marked 🧑): logging in to Claude or Codex, and approving the T3 Connect link. Only you can do those. The agent waits until you say you are done, then runs that step's check.

## Steps

### 1. Admin: create the login and let it run at boot

```bash
sudo useradd -m -s /bin/bash alice      # skip if the person already has a login
sudo loginctl enable-linger alice       # their server keeps running after logout and starts at boot
```

Check: `loginctl show-user alice --property=Linger` prints `Linger=yes`.

### 2. You: get this repository

```bash
git clone https://github.com/gear2000/llm-config-setup.git ~/llm-config-setup
```

Check: `~/llm-config-setup/tools/t3-setup.sh` exists.

### 3. 🧑 You: log in to an agent, once

T3 runs the agents you are logged in to. Install at least one in your own login and sign in:

- **Claude Code:** install from <https://claude.com/product/claude-code>, then run `claude auth login`
- **Codex:** install from <https://developers.openai.com/codex/cli>, then run `codex login`

Both commands show a link to open in a browser. This is a human step: an agent prints the command and waits.

Check: `~/.claude/.credentials.json` exists (Claude) or `~/.codex/auth.json` exists (Codex).

### 4. You: run the setup script

```bash
bash ~/llm-config-setup/tools/t3-setup.sh
```

The script:
- installs T3 Code if it is missing
- picks a port
- starts the always-on service
- sets new threads to open in a git worktree
- signs you in to T3 Connect

Re-running it is safe.

- **Port.** Use the default (the first free port from 3773 up) unless several people share this server. In that case, give each person their own port and keep a list: `--port 3774`. A person whose server is stopped leaves their port looking free.
- **🧑 T3 Connect sign-in.** The script prints a link and a short code, then waits.
  1. Open the link on your phone or any browser.
  2. Sign in, or create your T3 login there.
  3. Check that the code matches, and approve. The script continues on its own.

  An agent hands you the link and code and waits.
- Set up the service now and connect later: `--no-connect`, then `t3 up --connect` when ready.

Check: the script ends with `All checks passed.` Re-check any time with `bash ~/llm-config-setup/tools/t3-setup.sh verify`. `t3 status` shows `T3 Connect` as on.

### 5. You (optional): GitHub

For pull requests and pushes, connect GitHub in **your** login. Pick one:

- run `gh auth login` on the server, or
- in the app, add a token under **Settings → Source control → GitHub**.

Check: `gh auth status` succeeds, or the app shows GitHub as connected.

### 6. 🧑 Your phone

1. Install **T3 Code** from the App Store or Google Play, or open <https://app.t3.codes>.
2. Sign in with the **same T3 login** you approved in step 4.
3. Pick your server from the list and start a thread.

Done when you can send a message and the agent answers.

## Day to day

| Task | Command (as your login) |
| --- | --- |
| Check everything | `bash ~/llm-config-setup/tools/t3-setup.sh verify` |
| Status and log path | `t3 service status` |
| Update T3 | `t3 update` |
| Change your port | `bash ~/llm-config-setup/tools/t3-setup.sh --port 3780` |
| Turn T3 Connect on again | `t3 up --connect` |
| Remove the service (keeps your threads) | `t3 service uninstall` |

## Notes

- Each person's threads, settings, worktrees and logins live in their own `~/.t3` and home folder. Other people on the server cannot read them while home folders are mode 750 (the Ubuntu default; check with `stat -c %a ~`).
- Each person uses their own agent subscription. One person's usage never counts against another's.
- A T3 login can link up to 3 machines (phones and browsers do not count). A paid T3 Chat plan raises the limit.
