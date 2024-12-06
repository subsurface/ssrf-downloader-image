#!/bin/bash

# find all the tags and loop over them, adding together the release notes
first="${1:-v1.0.0}"
last="${2:-HEAD}"
if ! git show "$first" &> /dev/null || ! git show "$last" &> /dev/null ; then
	echo "Cannot determine changelog"
	exit 0
fi
for tag in "$last" $(git tag --contains "$first" --no-contains "$last" --sort=-taggerdate --list v[123456789]\* | grep -v beta)
do
	git diff "${tag}..${last}" release-notes.md | grep '^+[^+>]' | cut -c 2-
	echo
	last="$tag"
done
if [[ $first = *beta* ]]
then
	echo "Changes since $first include"
	git diff "${first}..${last}" release-notes.md | grep '^+[^+>]' | cut -c 2-
fi
