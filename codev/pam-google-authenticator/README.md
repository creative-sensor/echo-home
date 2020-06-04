This page present 2FA configuration using pam google authenticator

```WARNING: make sure system clock is properly synchronized or entire OS may be locked up```

### GDM-2FA
```
   make gdm-2fa
```

### TTY-2FA
```
   make tty-2fa
```

### SSHD-2FA
```
   make sshd-2fa
```
- AuthenticationMethods: use public-key and OTP keyboard-input only

### SUDO: NOPASSWD
I don't think it necessary for sudo defense as the three 2FA above have provided secure perimeter


