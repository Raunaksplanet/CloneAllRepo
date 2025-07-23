#!/bin/bash

# Usage: ./clone_repos.sh <GitHub profile/org URL>
# Example: ./clone_repos.sh https://github.com/Raunaksplanet

print_help() {
  echo "Usage: $0 <GitHub profile/org URL>"
  echo
  echo "Example:"
  echo "  $0 https://github.com/Raunaksplanet"
  echo
  echo "Options:"
  echo "  -h        Show this help message"
  exit 0
}

# Show help
[[ "$1" == "-h" || "$1" == "--help" || -z "$1" ]] && print_help

# Extract username/org from URL
url="$1"
user_or_org=$(echo "$url" | awk -F'/' '{print $NF}')

if [[ -z "$user_or_org" ]]; then
  echo "[-] Invalid GitHub URL"
  exit 1
fi

mkdir -p "${user_or_org}-repos" && cd "${user_or_org}-repos" || exit 1

for page in {1..5}; do
  curl -s "https://api.github.com/users/$user_or_org/repos?per_page=100&page=$page" |
  grep -o "https://github.com/$user_or_org/[^\"']*" |
  while read repo; do
    name=$(basename "$repo")
    if [ -d "$name" ]; then
      echo "[+] Skipping '$name' (already exists)"
      continue
    fi
    GIT_ASKPASS=true git clone "$repo.git"
  done
done
