#!/bin/sh
#
# A simple wrapper around flake8 which makes it possible
# to ask it to only verify files changed in the current
# git HEAD patch.
#
# Intended to be invoked via tox:
#
#   tox -eflake8 -- -HEAD
#
# Originally from the OpenStack project.

FLAKE_COMMAND="flake8 --max-line-length=120"

if test "$1" = "-HEAD" ; then
    shift
    # The dot is escaped so the pattern matches a .py suffix rather than
    # any character followed by "py".
    files=$(git diff --name-only HEAD~1 | grep -E '\.py$')
    if [ -z "${files}" ]; then
        echo "No python files in change."
        exit 0
    fi

    # A change which deletes or renames a python file names a path which
    # is no longer on disk. flake8 exits non-zero on a missing file, so
    # the whole lane fails on a change nobody could have linted.
    filtered_files=""
    for file in $files; do
        if [ -e "$file" ]; then
            if [ -z "${filtered_files}" ]; then
                filtered_files="${file}"
            else
                filtered_files="${filtered_files} ${file}"
            fi
        else
            echo "$file does not exist in the end state, skipping."
        fi
    done

    # Every candidate can vanish, if the change deleted python files
    # rather than editing them. Stop here rather than falling through:
    # the command below treats an empty file list as "no arguments",
    # and flake8 with no arguments walks the whole tree.
    if [ -z "${filtered_files}" ]; then
        echo "No python files remain in the end state."
        exit 0
    fi

    echo "Running flake8 on ${filtered_files}"
    # flake8 is given the paths as arguments. The diff which used to be
    # piped in here was for "flake8 --diff", which flake8 6 removed;
    # flake8 ignores stdin once it has file operands, so the diff was
    # generated, thrown away, and on a large change left to take SIGPIPE.
    #
    # The word splitting is deliberate: filtered_files is a list of
    # paths, and quoting it would make the whole list one filename.
    # shellcheck disable=SC2086
    $FLAKE_COMMAND ${filtered_files}
else
    echo "Running flake8 on all files"
    exec $FLAKE_COMMAND "$@"
fi
