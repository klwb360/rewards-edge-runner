#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$project_root"

launcher_args=("$@")
argument_index=0
launcher_command=""
while (( argument_index < ${#launcher_args[@]} )); do
    argument="${launcher_args[$argument_index]}"
    case "$argument" in
        --config) argument_index=$((argument_index + 2)) ;;
        --config=*) argument_index=$((argument_index + 1)) ;;
        *) launcher_command="$argument"; break ;;
    esac
done

if [[ "$launcher_command" == "setup" ]]; then
    exec python3 "$project_root/linux_cli.py" "$@"
fi

python_path="$project_root/.venv/bin/python"
if [[ ! -x "$python_path" ]]; then
    printf '%s\n' '缺少 Linux Python 环境。请先运行：bash Rewards.sh setup' >&2
    exit 1
fi
exec "$python_path" "$project_root/linux_cli.py" "$@"
