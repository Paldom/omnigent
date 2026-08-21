# The bench refused every own_auth native without gateway credentials, so the
# real reason a harness could not run was hidden behind a credentials error the
# contributor could do nothing about.
#
#   vhs recordings/bench-live-gate.tape

Output recordings/out/bench-live-gate.gif
Output recordings/out/bench-live-gate.mp4

Set Shell bash
Set FontSize 15
Set Width 1400
Set Height 620
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 18ms

Hide
Type "export PS1='\[\033[38;5;110m\]$ \[\033[0m\]' && clear" Enter
Type "unset OPENAI_API_KEY OPENAI_BASE_URL" Enter
Type "cd /tmp/demo-main" Enter
Type "clear" Enter
Show

Type "# No Databricks profile, no OPENAI_API_KEY. On main:"
Enter
Sleep 1200ms
Type "python -m tests.harness_bench --harness goose-native --live"
Enter
Sleep 3s

Type "# A credentials error — for a harness that never uses the gateway."
Enter
Sleep 2s
Type "# goose logs its own model in. So does cursor, kimi, pi, and four more."
Enter
Sleep 2500ms

Hide
Type "cd /tmp/demo-fix && clear" Enter
Show

Type "# With the fix, same machine, same missing credentials:"
Enter
Sleep 1200ms
Type "python -m tests.harness_bench --harness goose-native --live"
Enter
Sleep 3s

Type "# Now it says the thing you can actually act on."
Enter
Sleep 3s
