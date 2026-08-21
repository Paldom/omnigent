# GET /v1/harnesses gained four capability axes. No UI renders them, so the
# demo is the API response: the same request against a server on main and one
# on this branch. Every axis ships as `unknown` on purpose — the point is that
# a consumer can now ask the question at all, and the bench has somewhere to
# write the answer.
#
#   vhs recordings/harness-capability-axes.tape
#
# Needs a server on :7799 (main) and :7798 (this branch). The three helper
# scripts are one curl+jq each; see the PR body.

Output "recordings/out/harness-capability-axes.gif"
Output "recordings/out/harness-capability-axes.mp4"

Set Shell bash
Set FontSize 16
Set Width 1240
Set Height 520
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 22ms

Hide
Type "cd /tmp/capdemo && export PS1='\[\033[38;5;110m\]$ \[\033[0m\]' && clear" Enter
Show

Type "# On main: can a client ask whether a harness supports skills or plan mode?"
Enter
Sleep 1400ms
Type "./axes.sh 7799"
Enter
Sleep 3500ms

Type "# All null. The keys do not exist, so the question is unanswerable."
Enter
Sleep 2500ms

Hide
Type "clear" Enter
Show

Type "# Same request, same harness, a server running this branch:"
Enter
Sleep 1400ms
Type "./one.sh 7798"
Enter
Sleep 4s

Type "# Four fields per axis. 'unknown' is the honest default until the bench"
Enter
Sleep 800ms
Type "# observes a harness -- a declaration nobody measured is exactly how a"
Enter
Sleep 800ms
Type "# parity matrix stops being true."
Enter
Sleep 2500ms

Hide
Type "clear" Enter
Show

Type "# Every harness carries the axes, not just one:"
Enter
Sleep 1400ms
Type "./all.sh 7798"
Enter
Sleep 5s
