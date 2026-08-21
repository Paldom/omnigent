#!/bin/bash
curl -s "localhost:$1/v1/harnesses" | jq '.data[0].capabilities.plan_mode'
