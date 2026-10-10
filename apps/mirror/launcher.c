// voice-lab-mirror-launcher: the TCC-stable identity of the Voice Memos mirror.
//
// macOS attributes a child process's privacy (TCC) checks, like reading the Voice
// Memos group container, to the RESPONSIBLE process, which for a launchd job is the
// job's own executable. Granting Full Disk Access to Python directly would (a) grant
// every Python script on the machine and (b) silently break on every interpreter
// upgrade, because the grant binds to the exact binary.
//
// This launcher is the durable alternative: a tiny compiled binary that never needs to
// change. It SPAWNS the mirror as a child, deliberately not exec(), which would replace
// this process's code identity with Python's, then forwards launchd's shutdown signals
// and mirrors the child's exit status (3 = the drop was locked, retry next period).
// Full Disk Access is granted to THIS binary, once.
//
// MODES. The first argument selects what to run, from a fixed allowlist; the launcher
// never builds a command from its arguments:
//   mirror  ->  VL_MIRROR_PYTHON -I -m voice_lab_mirror --approval VL_MIRROR_APPROVAL
// No argument, any other argument, or any extra argument is refused with exit 64
// (EX_USAGE) and nothing is spawned. The interpreter path and the standing approval
// are compiled in; everything that may change (the target, the key, the rsync binary,
// the state directory) reaches the mirror through the environment launchd passes on
// from the plist, so retargeting the mirror needs no rebuild and no new grant.
// `-I` keeps PYTHONPATH and the user site out: the module comes from the
// interpreter's own environment (a venv holding only this package).
//
// Build and sign (apps/mirror/README.md):
//   cc -O2 -Wall -Werror -DVL_MIRROR_PYTHON="\"$VENV/bin/python3\""
//      -o "$BIN/voice-lab-mirror-launcher" apps/mirror/launcher.c   (one line)
//   codesign -s - -f -i com.scshafe.voice-lab.mirror-launcher "$BIN/voice-lab-mirror-launcher"
// Recompiling changes the ad-hoc signature and therefore needs the grant re-ticked;
// the point of this file is that it has no reason to change.

#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef VL_MIRROR_PYTHON
#error "define VL_MIRROR_PYTHON as the absolute path of the mirror venv's python3"
#endif

#ifndef VL_MIRROR_APPROVAL
#define VL_MIRROR_APPROVAL \
  "owner standing approval: Voice Memos mirror to the voice-lab drop (VOICE-PLATFORM.md 5.5)"
#endif

extern char **environ;

struct mode {
  const char *name;
  const char *const *argv;
};

static const char *const MIRROR_ARGV[] = {
  VL_MIRROR_PYTHON, "-I", "-m", "voice_lab_mirror", "--approval", VL_MIRROR_APPROVAL, NULL,
};

static const struct mode MODES[] = {
  { "mirror", MIRROR_ARGV },
};

// Returns the mode the arguments name, or NULL unless they are exactly one
// allowlisted mode name.
const struct mode *resolve_mode(int argc, char *const argv[]) {
  if (argc != 2) return NULL;
  for (size_t i = 0; i < sizeof(MODES) / sizeof(MODES[0]); i++) {
    if (strcmp(argv[1], MODES[i].name) == 0) return &MODES[i];
  }
  return NULL;
}

#ifndef VL_LAUNCHER_NO_MAIN
static pid_t child_pid = 0;

static void forward_signal(int signum) {
  if (child_pid > 0) kill(child_pid, signum);
}

int main(int argc, char *argv[]) {
  const struct mode *mode = resolve_mode(argc, argv);
  if (mode == NULL) {
    fprintf(stderr, "voice-lab-mirror-launcher: usage: voice-lab-mirror-launcher mirror\n");
    return 64; /* EX_USAGE */
  }

  struct sigaction action;
  sigemptyset(&action.sa_mask);
  action.sa_flags = 0;
  action.sa_handler = forward_signal;
  sigaction(SIGTERM, &action, NULL);
  sigaction(SIGINT, &action, NULL);
  sigaction(SIGHUP, &action, NULL);

  int rc = posix_spawn(&child_pid, mode->argv[0], NULL, NULL, (char *const *)mode->argv, environ);
  if (rc != 0) {
    fprintf(stderr, "voice-lab-mirror-launcher: posix_spawn(%s) failed: errno %d\n",
            mode->argv[0], rc);
    return 78; /* EX_CONFIG */
  }

  int status = 0;
  while (waitpid(child_pid, &status, 0) == -1) {
    if (errno != EINTR) {
      perror("voice-lab-mirror-launcher: waitpid");
      return 71; /* EX_OSERR */
    }
  }
  if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
  return WEXITSTATUS(status);
}
#endif /* VL_LAUNCHER_NO_MAIN */
