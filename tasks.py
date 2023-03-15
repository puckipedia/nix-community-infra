#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, List

from deploykit import DeployGroup, DeployHost
from invoke import task

ROOT = Path(__file__).parent.resolve()
os.chdir(ROOT)

# Deploy to all hosts in parallel
def deploy_nixos(hosts: List[DeployHost]) -> None:

    g = DeployGroup(hosts)

    res = subprocess.run(
        ["nix", "flake", "metadata", "--json"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    data = json.loads(res.stdout)
    path = data["path"]

    def deploy(h: DeployHost) -> None:
        target = f"{h.user or 'root'}@{h.host}"
        h.run_local(f"rsync -vaF --delete -e ssh {path}/ {target}:/etc/nixos")

        h.run("nixos-rebuild switch --option accept-flake-config true")

    g.run_function(deploy)


def sfdisk_json(host: DeployHost, dev: str) -> List[Any]:
    out = host.run(f"sfdisk --json {dev}", stdout=subprocess.PIPE)
    data = json.loads(out.stdout)
    return data["partitiontable"]["partitions"]


def _format_disks(host: DeployHost, devices: List[str]) -> None:
    assert (
        len(devices) == 1 or len(devices) == 2
    ), "we only support single devices or mirror raids at the moment"
    # format disk with as follow:
    # - partition 1 will be the boot partition, needed for legacy (BIOS) boot
    # - partition 2 is for boot partition
    # - partition 3 takes up the rest of the space and is for the system
    for device in devices:
        host.run(
            f"sgdisk -Z -n 1:2048:4095 -n 2:4096:+2G -N 3 -t 1:ef02 -t 2:8304 -t 3:8304 {device}"
        )

    # create mdadm raid for /boot with ext4
    if len(devices) == 2:
        boot_parts = []
        root_parts = []
        for dev in devices:
            # use partuuids as they are more stable than device names
            partitions = sfdisk_json(host, dev)
            boot_parts.append(partitions[1]["node"])
            root_parts.append(f"/dev/disk/by-partuuid/{partitions[2]['uuid'].lower()}")

        host.run(
            f"mdadm --create --verbose /dev/md127 --raid-devices=2 --level=1 {' '.join(boot_parts)}"
        )
        host.run(
            f"zpool create zroot -O acltype=posixacl -O xattr=sa -O compression=lz4 mirror {' '.join(root_parts)}"
        )
        boot = "/dev/md127"
    else:
        partitions = sfdisk_json(host, devices[0])
        boot = partitions[1]["node"]
        uuid = partitions[2]["uuid"].lower()
        root_part = f"/dev/disk/by-partuuid/{uuid}"
        host.run(
            f"zpool create zroot -O acltype=posixacl -O xattr=sa -O compression=lz4 -O atime=off {root_part}"
        )

    host.run("partprobe")
    host.run(f"mkfs.ext4 -F {boot}")

    # setup zfs dataset
    host.run("zfs create -o mountpoint=none zroot/root")
    host.run("zfs create -o mountpoint=legacy zroot/root/nixos")
    host.run("zfs create -o mountpoint=legacy zroot/root/home")

    ## and finally mount
    host.run("mount -t zfs zroot/root/nixos /mnt")
    host.run("mkdir /mnt/home /mnt/boot")
    host.run("mount -t zfs zroot/root/home /mnt/home")
    host.run("mount -t ext4 /dev/md127 /mnt/boot")


@task
def update_hound_repos(c):
    """
    Update list of repos for hound search
    """

    def all_for_org(org):
        import requests

        github_token = os.environ.get("GITHUB_TOKEN")

        disallowed_repos = [
            "nix-community/dream2nix-auto-test",
            "nix-community/image-spec",
            "nix-community/nix",
            "nix-community/nixpkgs",
            "nix-community/nsncd",
            "nix-community/rkwifibt",
            "NixOS/nixops-dashboard",  # empty repo causes an error
        ]

        resp = {}

        next_url = "https://api.github.com/orgs/{}/repos".format(org)
        while next_url is not None:

            if github_token is not None:
                headers = {"Authorization": f"token {github_token}"}
                repo_resp = requests.get(next_url, headers=headers)
            else:
                repo_resp = requests.get(next_url)

            if "next" in repo_resp.links:
                next_url = repo_resp.links["next"]["url"]
            else:
                next_url = None

            repos = repo_resp.json()

            resp.update(
                {
                    "{}-{}".format(org, repo["name"]): {
                        "url": repo["clone_url"],
                    }
                    for repo in repos
                    if repo["full_name"] not in disallowed_repos
                    if repo["archived"] is False
                }
            )

        return resp

    repos = {**all_for_org("NixOS"), **all_for_org("nix-community")}

    with open("services/hound/hound.json", "w") as f:
        f.write(
            json.dumps(
                {
                    "max-concurrent-indexers": 1,
                    "dbpath": "/var/lib/hound/data",
                    "repos": repos,
                    "vcs-config": {"git": {"detect-ref": True}},
                },
                indent=2,
                sort_keys=True,
            )
        )
        f.write("\n")


@task
def update_sops_files(c):
    """
    Update all sops yaml and json files according to .sops.yaml rules
    """
    c.run(
        """
find . \
        -type f \
        \( -iname '*.enc.json' -o -iname 'secrets.yaml' \) \
        -exec sops updatekeys --yes {} \;
"""
    )


@task
def scan_age_keys(c, host):
    """
    Scans for the host key via ssh an converts it to age. Use inv scan-age-keys build**.nix-community.org
    """
    proc = subprocess.run(
        ["ssh-keyscan", host], stdout=subprocess.PIPE, text=True, check=True
    )
    print("###### Age keys ######")
    subprocess.run(
        ["ssh-to-age"],
        input=proc.stdout,
        check=True,
        text=True,
    )


@task
def update_terraform(c):
    """
    Update terraform devshell flake
    """
    with c.cd("terraform"):
        c.run(
            """
system="$(nix eval --impure --raw --expr 'builtins.currentSystem')"
old="$(nix build --no-link --print-out-paths ".#devShells.${system}.default")"
nix flake update --commit-lock-file
new="$(nix build --no-link --print-out-paths ".#devShells.${system}.default")"
commit="$(git log --pretty=format:%B -1)"
diff="$(nix store diff-closures "${old}" "${new}" | awk -F ',' '/terraform/ && /→/ {print $1}')"
git commit --amend -m "${commit}" -m "Terraform updates:" -m "${diff}"
"""
        )


@task
def format_disks(c, hosts="", disks=""):
    """
    Format disks with zfs, i.e.: inv format-disks --hosts build02 --disks /dev/nvme0n1,/dev/nvme1n1
    """
    for h in get_hosts(hosts):
        _format_disks(h, disks.split(","))


@task
def setup_secret(c, hosts=""):
    """
    Setup SSH key and print age key for sops-nix
    """
    for h in get_hosts(hosts):
        h.run(
            "install -m600 -D /etc/ssh/ssh_host_rsa_key /mnt/etc/ssh/ssh_host_rsa_key"
        )
        h.run(
            "install -m600 -D /etc/ssh/ssh_host_ed25519_key /mnt/etc/ssh/ssh_host_ed25519_key"
        )
        print(h.host)
        h.run(
            "nix-shell -p ssh-to-age --run 'cat /etc/ssh/ssh_host_ed25519_key.pub | ssh-to-age'"
        )


@task
def nixos_install(c, hosts=""):
    """
    Run NixOS install
    """
    for h in get_hosts(hosts):
        h.run(
            "nix-shell -p git --run 'git clone https://github.com/nix-community/infra && cd infra && nix-shell'"
        )
        hostname = h.host.replace(".nix-community.org", "")
        h.run(
            f"cd /root/infra && nixos-install --system $(nix-build -A {hostname}-system)"
        )


def get_hosts(hosts: str) -> List[DeployHost]:
    if hosts == "":
        return [
            DeployHost(f"build{n + 1:02d}.nix-community.org", user="root")
            for n in range(4)
        ]

    return [DeployHost(f"{h}.nix-community.org", user="root") for h in hosts.split(",")]


@task
def deploy(c, hosts=""):
    """
    Deploy to all servers. Use inv deploy --hosts build01 to deploy to a single server
    """
    deploy_nixos(get_hosts(hosts))


@task
def build_local(c, hosts=""):
    """
    Build all servers. Use inv build-local --hosts build01 to build a single server
    """
    g = DeployGroup(get_hosts(hosts))

    def build_local(h: DeployHost) -> None:
        h.run_local(
            [
                "nixos-rebuild",
                "build",
                "--option",
                "accept-flake-config",
                "true",
                "--flake",
                f".#{h.host}",
            ]
        )

    g.run_function(build_local)


def wait_for_port(host: str, port: int, shutdown: bool = False) -> None:
    import socket
    import time

    while True:
        try:
            with socket.create_connection((host, port), timeout=1):
                if shutdown:
                    time.sleep(1)
                    sys.stdout.write(".")
                    sys.stdout.flush()
                else:
                    break
        except OSError:
            if shutdown:
                break
            else:
                time.sleep(0.01)
                sys.stdout.write(".")
                sys.stdout.flush()


@task
def reboot(c, hosts=""):
    """
    Reboot hosts. example usage: inv reboot --hosts build01,build02
    """
    for h in get_hosts(hosts):
        h.run("reboot &")

        print(f"Wait for {h.host} to shutdown", end="")
        sys.stdout.flush()
        wait_for_port(h.host, h.port, shutdown=True)
        print("")

        print(f"Wait for {h.host} to start", end="")
        sys.stdout.flush()
        wait_for_port(h.host, h.port)
        print("")


@task
def cleanup_gcroots(c, hosts=""):
    g = DeployGroup(get_hosts(hosts))
    g.run("find /nix/var/nix/gcroots/auto -type s -delete")
    g.run("systemctl restart nix-gc")
