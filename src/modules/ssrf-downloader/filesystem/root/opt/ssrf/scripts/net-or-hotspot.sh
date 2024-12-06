#!/bin/bash

# this needs to run as root
if [ "$(id -u)" != "0" ] ; then
    echo "this command requires superuser privileges - please run as sudo bash $0"
    exit 1
fi

function test_network() {
    pids=()

    TIMEOUT=5
    sleep $TIMEOUT &

    # is there a gateway?
    gateway=$(ip route | awk '/default/ { print $3 }')
    if [[ $gateway != "" ]]; then
        ping -c 1 -W $TIMEOUT "$gateway" &> /dev/null &
        pids+=($!)
    fi

    ping -c 1 -W $TIMEOUT 8.8.8.8 &> /dev/null &
    pids+=($!)

    curl --max-time $TIMEOUT akamai.com &> /dev/null &
    pids+=($!)

    pgrep tailscaled &>/dev/null && tailscale status --json 2>/dev/null | jq -r .TailscaleIPs | grep -qs -v -e null &
    pids+=($!)

    pgrep zerotier-one &>/dev/null && zerotier-cli status 2>&1 | grep -qs -e ONLINE &
    pids+=($!)

    # wait returns zero for a specific backgrounded pid when the exit status for that pid was zero
    # this also works for pids that have already exited when wait is called
    for pid in ${pids[@]}; do
        if wait $pid; then
            return 0
        fi
    done

    wait
    return 1
}
function check_network() {
    for i in {1..6}; do
        if test_network; then
            return 0
        fi
    done
    return 1
}

if check_network; then
    echo "network reachable, no need to start an access point"
    # out of an abundance of caution make sure these services are not enabled:
    for service in hostapd.service isc-dhcp-server.service; do
        if systemctl is-enabled "$service" &>/dev/null || ! [[ -L /etc/systemd/system/hostapd.service ]]; then
            echo "stopping / disabling / masking $service"
            systemctl stop "$service"
            systemctl disable "$service"
            systemctl mask "$service"
        fi
    done
    exit 0
fi

# that's not good, let's try to start an access point

wlan=$(iw dev | grep Interface | cut -d' ' -f2)
if [[ $wlan == "" ]] ; then
    echo "No wireless interface detected, giving up"
    exit 1
fi

cp /opt/ssrf/accesspoint/hostapd.conf /etc/hostapd/hostapd.conf
cp /opt/ssrf/accesspoint/dhcpd.conf /etc/dhcp/dhcpd.conf
cp /opt/ssrf/accesspoint/isc-dhcp-server /etc/default/isc-dhcp-server

if [[ $wlan != "wlan0" ]] ; then
    sed -i "s/wlan0/$wlan/g" /etc/default/isc-dhcp-server
    sed -i "s/wlan0/$wlan/g" /etc/hostapd/hostapd.conf
fi

systemctl unmask hostapd.service isc-dhcp-server.service &>/dev/null
for service in hostapd.service isc-dhcp-server.service; do
    if systemctl is-enabled "$service" &>/dev/null; then
        systemctl disable "$service"
    fi
done

while true; do
    echo "No internet connection detected, starting access point"
    python3 /opt/ssrf/ssrf-downloader/hotspot-app.py "$wlan"

    if check_network; then
        # break outer loop as well if network tests good
        break
    fi
done

# systemctl disable takes 8 seconds for these 2 services,
# stopping them and masking them is sufficient to stop them from starting so do that instead
systemctl stop hostapd.service isc-dhcp-server.service &>/dev/null
systemctl mask hostapd.service isc-dhcp-server.service &>/dev/null

echo "successfully connected to network"
