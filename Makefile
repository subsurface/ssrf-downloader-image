HOST ?= ssrf-downloader.local
SSH_CONTROL=/tmp/ssrf-downloader-ssh-control-${HOST}

sync-and-update:
# sync relevant files and update
	# sync over changes from local repo
	make sync-py-control

	# restart webinterface
	ssh -S "${SSH_CONTROL}" root@$(HOST) systemctl restart ssrf-downloader.service

sync-py-control:
# check if the SSH control port is open, if not, open it.
	ssh -O check -S "${SSH_CONTROL}" root@$(HOST) || make ssh-control
	rsync -av \
	--delete --exclude="*.pyc" --progress \
	-e "ssh -S ${SSH_CONTROL}" \
	src/modules/ssrf-downloader/filesystem/root/opt/ssrf/ssrf-downloader/ \
	root@$(HOST):/opt/ssrf/ssrf-downloader/

	rsync -av \
	--exclude="*.pyc" --progress \
	-e "ssh -S ${SSH_CONTROL}" \
	src/modules/ssrf-downloader/filesystem/root/opt/ssrf/ \
	root@$(HOST):/opt/ssrf/

	mkdir -p src/modules/ssrf-downloader/filesystem/root/usr/bin
	rsync -av \
	--exclude="*.pyc" --progress \
	-e "ssh -S ${SSH_CONTROL}" \
	src/modules/ssrf-downloader/filesystem/root/usr/bin/ \
	root@$(HOST):/usr/bin/

	rsync -av \
	--exclude="*.pyc" --progress \
	-e "ssh -S ${SSH_CONTROL}" \
	src/modules/ssrf-downloader/filesystem/root/etc/ \
	root@$(HOST):/etc/

ssh-control:
# to avoid having to SSH every time,
# we make a SSH control port to use with rsync.
	ssh -M -S "${SSH_CONTROL}" -fnNT root@$(HOST)

