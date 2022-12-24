# .bashrc

# Source global definitions
if [ -f /etc/bashrc ]; then
	. /etc/bashrc
fi

# Uncomment the following line if you don't like systemctl's auto-paging feature:
# export SYSTEMD_PAGER=

# User specific aliases and functions

# ---- FUNCTION ----
function terminame {
    # set terminal name
    echo -ne "\033]0;${1}\007"
}


# ---- ALIAS ----
#alias vim='vimx'
alias git-glog='git log --all --decorate --oneline --graph'
alias gitroot='cd $(git rev-parse --show-toplevel)'
alias grin='grep -rin '
alias ll='ls -la '
alias gvim='terminame "VIM | $(basename $PWD)" ; /c/Program\ Files\ \(x86\)/Vim/vim90/gvim.exe'

# ---- LOCAL ----
export LOCAL_BIN=~/.local/bin
export PYTHONPATH=/c/Users/creativ/AppData/Local/Programs/Python/Python310
PATH=$PATH:$LOCAL_BIN:$PYTHONPATH

COLOR1='\[\033[38;5;34m\]'
COLOR2='\[\033[38;5;161m\]'
BOLD='\[\033[1m\]' # Work only if "allow bold text" setting is enabled
STOP='\[\033[0m\]'

PS1="[${BOLD}${COLOR1}\u${STOP}@${BOLD}${COLOR2}\h \W${STOP}]\$ "


# ---- EDIT ----
source ~/.bashrc.edit
