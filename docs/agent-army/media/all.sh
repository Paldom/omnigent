#!/bin/bash
curl -s "localhost:$1/v1/harnesses" | jq -r '.data[] | "\(.id)\t skills=\(.capabilities.skills.support)  plan_mode=\(.capabilities.plan_mode.support)"' | column -t -s $'\t'
