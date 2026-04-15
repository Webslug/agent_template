#!/bin/bash
# =============================================================================
# get_user.sh
# Returns the username of the first graphical session user found.
#
#
# TEST:
#   bash /home/kim/projects/template/get_user.sh
# =============================================================================

# First pass: look for the graphical seat (:0) specifically
# `who` line format: kim  tty1  2026-03-22 00:04 (:0)
while IFS= read -r line; do
    if echo "$line" | grep -q '(:0)'; then
        echo "$line" | awk '{print $1}'
        exit 0
    fi
done < <(who 2>/dev/null)

# Second pass: just take the first user listed in `who`
first=$(who 2>/dev/null | awk 'NR==1{print $1}')
if [ -n "$first" ]; then
    echo "$first"
    exit 0
fi

echo "unknown"
exit 0
