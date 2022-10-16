# .bashrc

# Source global definitions
if [ -f /etc/bashrc ]; then
	. /etc/bashrc
fi

# Uncomment the following line if you don't like systemctl's auto-paging feature:
# export SYSTEMD_PAGER=

# User specific aliases and functions

# ---- ALIAS ----
alias vim='vimx'
alias git-glog='git log --all --decorate --oneline --graph'
alias gitroot='cd $(git rev-parse --show-toplevel)'
alias grin='grep -rin '


# ---- LOCAL ----
PATH=$PATH:~/.local/bin

COLOR1='\[\033[38;5;34m\]'
COLOR2='\[\033[38;5;161m\]'
BOLD='\[\033[1m\]' # Work only if "allow bold text" setting is enabled
STOP='\[\033[0m\]'

PS1="[${BOLD}${COLOR1}\u${STOP}@${BOLD}${COLOR2}\h \W${STOP}]\$ "
PROMPT_COMMAND='echo -ne "\033]0;$(basename $PWD)\007"'


# ---- EXPORT ----
#export MOZILLA_FIREFOX_PROFILE=
#export MOZILLA_THUNDERBIRD_PROFILE=
#export GNUPGHOME=

# ---- EDIT ----
test -r ~/.bashrc.edit && source ~/.bashrc.edit
