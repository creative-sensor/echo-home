# macos : real bash

--------------------------------

### 0 DESCRIPTION

### 1 SOLUTION

### 2 NOTES

### 3 TEST/VERIFICATION

### 4 JOURNAL

---

### 🧱 Step-by-Step: Build Bash from Source on macOS

#### 1. **Download the Latest Bash Source**

Visit [GNU Bash FTP](https://ftp.gnu.org/gnu/bash/) and grab the latest `.tar.gz` version. Or use `curl`:

```bash
cd /tmp
curl -O https://ftp.gnu.org/gnu/bash/bash-5.2.15.tar.gz
```

> Replace `5.2.15` with the latest version if needed.

#### 2. **Extract and Enter the Directory**

```bash
tar xzf bash-5.2.15.tar.gz
cd bash-5.2.15
```

#### 3. **Configure the Build**

```bash
./configure --prefix=/usr/local
```

This sets the install location to `/usr/local/bin/bash`.

#### 4. **Compile Bash**

```bash
make
```

This may take a few minutes depending on your system.

#### 5. **Install Bash**

```bash
sudo make install
```

Now Bash is installed at `/usr/local/bin/bash`.

---

### 🧼 Final Setup

#### ✅ Add Bash to Allowed Shells

```bash
sudo sh -c 'echo /usr/local/bin/bash >> /etc/shells'
```

#### 🔄 Change Your Default Shell

```bash
chsh -s /usr/local/bin/bash
```

Then restart your terminal or log out and back in.

#### 🔍 Verify the Version

```bash
echo $BASH_VERSION
```

You should see something like `5.2.15`.

---

### 🧠 Bonus: Make Bash Feel Like Linux

Add this to your `~/.bashrc`:

```bash
shopt -s globstar
shopt -s checkwinsize
shopt -s histappend
alias ls='ls -G'
alias ll='ls -la'
export HISTSIZE=10000
export HISTFILESIZE=20000
```

Then source it:

```bash
source ~/.bashrc
```

--------------------------------

```json
{ "project_code": "APLE" , "links": "" , "location": "" , "fpoint": "" }
```
