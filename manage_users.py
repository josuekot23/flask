#!/usr/bin/env python3
"""
Gestion des comptes utilisateurs SondeX (fichier users.json).

Usage:
    python3 manage_users.py add <username> --role user|admin
    python3 manage_users.py remove <username>
    python3 manage_users.py list

Le mot de passe est toujours saisi de façon masquée (jamais en argument de
ligne de commande, pour ne pas se retrouver dans l'historique du shell).
"""

import argparse
import getpass
import sys

from werkzeug.security import generate_password_hash

import rf_data as rf


def cmd_add(args):
    users = rf.load_users()
    if args.username in users and not args.force:
        print(f"L'utilisateur '{args.username}' existe déjà. Utilise --force pour écraser.")
        sys.exit(1)

    password = getpass.getpass("Mot de passe: ")
    confirm = getpass.getpass("Confirmer: ")
    if password != confirm:
        print("Les mots de passe ne correspondent pas.")
        sys.exit(1)
    if len(password) < 8:
        print("Le mot de passe doit faire au moins 8 caractères.")
        sys.exit(1)

    users[args.username] = {
        "password_hash": generate_password_hash(password),
        "role": args.role,
    }
    rf.save_users(users)
    print(f"Utilisateur '{args.username}' ({args.role}) enregistré.")


def cmd_remove(args):
    users = rf.load_users()
    if args.username not in users:
        print(f"Utilisateur '{args.username}' introuvable.")
        sys.exit(1)
    del users[args.username]
    rf.save_users(users)
    print(f"Utilisateur '{args.username}' supprimé.")


def cmd_list(args):
    users = rf.load_users()
    if not users:
        print("Aucun utilisateur enregistré.")
        return
    for username, info in users.items():
        print(f"  {username:20s} role={info.get('role')}")


def main():
    parser = argparse.ArgumentParser(description="Gestion des comptes SondeX")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Ajouter ou mettre à jour un utilisateur")
    p_add.add_argument("username")
    p_add.add_argument("--role", choices=["user", "admin"], default="user")
    p_add.add_argument("--force", action="store_true", help="Écraser si l'utilisateur existe déjà")
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="Supprimer un utilisateur")
    p_remove.add_argument("username")
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="Lister les utilisateurs")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
