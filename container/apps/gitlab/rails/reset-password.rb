USERNAME = ARGV[0]
user = User.find_by_username USERNAME
new_password = ::User.random_password
user.password = new_password
user.password_confirmation = new_password
user.save!

File.open("/tmp/password_reset","w") do |f|
 f.puts new_password
end
