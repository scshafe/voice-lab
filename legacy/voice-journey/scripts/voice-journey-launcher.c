// voice-journey-launcher — the TCC-stable identity for the Voice Journey service.
//
// macOS attributes a child process's privacy (TCC) checks — like reading the
// Voice Memos group container — to the RESPONSIBLE process, which for a launchd
// service is the service's own executable. Granting Full Disk Access to `node`
// directly (a) over-grants every node script on the machine and (b) silently
// breaks on every brew upgrade, because the grant binds to the exact binary.
//
// This launcher is the durable alternative: a tiny compiled binary that never
// needs to change. It SPAWNS the real launch script as a child — deliberately
// not exec(), which would replace this process's code identity with node's —
// then forwards launchd's shutdown signals and mirrors the child's exit status.
// Full Disk Access is granted to THIS binary, once.
//
// MODES. The first argument selects what to run, from a fixed allowlist; the
// launcher never builds a command from its arguments:
//   (none) | browser  -> voice-journey-browser-launchd   (the default, unchanged)
//   mirror            -> voice-journey-mirror-launchd    (phase 3: corpus mirror)
// Any other argument, or any extra argument, is refused with exit 64
// (EX_USAGE) and nothing is spawned. The child always gets argv = { target }:
// flags (such as the mirror's --approval) live in the wrapper script, never
// here. Both modes run under this one binary, so ONE Full Disk Access grant
// covers the browser and the mirror.
//
// Build + deploy (also in README "Corpus access"):
//   clang -O2 -o ~/.mission-control/bin/voice-journey-launcher scripts/voice-journey-launcher.c
//   codesign -s - -f -i ai.voice-journey.launcher ~/.mission-control/bin/voice-journey-launcher
// (Both wrappers, voice-journey-browser-launchd and voice-journey-mirror-launchd,
// are installed beside it; see README "Corpus mirror".)
// Recompiling changes the ad-hoc signature and therefore needs the grant
// re-ticked — the point of this file is that it has no reason to change.

#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

struct mode {
  const char *name;
  const char *target;
};

static const struct mode MODES[] = {
  { "browser", "/Users/cole/.mission-control/bin/voice-journey-browser-launchd" },
  { "mirror", "/Users/cole/.mission-control/bin/voice-journey-mirror-launchd" },
};

// Returns the fixed target for the requested mode, or NULL if the arguments are
// not exactly "" or one allowlisted mode name.
const char *resolve_mode(int argc, char *const argv[]) {
  if (argc == 1) return MODES[0].target; /* no argument: browser, as before */
  if (argc != 2) return NULL;
  for (size_t i = 0; i < sizeof(MODES) / sizeof(MODES[0]); i++) {
    if (strcmp(argv[1], MODES[i].name) == 0) return MODES[i].target;
  }
  return NULL;
}

#ifndef VJ_LAUNCHER_NO_MAIN
static pid_t child_pid = 0;

static void forward_signal(int signum) {
  if (child_pid > 0) kill(child_pid, signum);
}

int main(int argc, char *argv[]) {
  const char *TARGET = resolve_mode(argc, argv);
  if (TARGET == NULL) {
    fprintf(stderr, "voice-journey-launcher: usage: voice-journey-launcher [browser|mirror]\n");
    return 64; /* EX_USAGE */
  }
  char *const child_argv[] = { (char *)TARGET, NULL };

  struct sigaction action;
  sigemptyset(&action.sa_mask);
  action.sa_flags = 0;
  action.sa_handler = forward_signal;
  sigaction(SIGTERM, &action, NULL);
  sigaction(SIGINT, &action, NULL);
  sigaction(SIGHUP, &action, NULL);

  int rc = posix_spawn(&child_pid, TARGET, NULL, NULL, child_argv, environ);
  if (rc != 0) {
    fprintf(stderr, "voice-journey-launcher: posix_spawn(%s) failed: errno %d\n", TARGET, rc);
    return 78; /* EX_CONFIG */
  }

  int status = 0;
  while (waitpid(child_pid, &status, 0) == -1) {
    if (errno != EINTR) {
      perror("voice-journey-launcher: waitpid");
      return 71; /* EX_OSERR */
    }
  }
  if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
  return WEXITSTATUS(status);
}
#endif /* VJ_LAUNCHER_NO_MAIN */
