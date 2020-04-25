provider "aws" {}

resource "aws_vpc" "{{PREFIX}}" {
  cidr_block       = "10.{{OCTET}}.0.0/16"

  tags = {
    Name = "{{PREFIX}}"
  }
}

resource "aws_subnet" "public_a" {
  vpc_id     = "${aws_vpc.{{PREFIX}}.id}"
  cidr_block = "10.{{OCTET}}.0.0/20"
  availability_zone = "ap-southeast-1a"
  tags = {
    Name = "{{PREFIX}}-public-a"
  }
}
resource "aws_subnet" "public_b" {
  vpc_id     = "${aws_vpc.{{PREFIX}}.id}"
  cidr_block = "10.{{OCTET}}.16.0/20"
  availability_zone = "ap-southeast-1b"
  tags = {
    Name = "{{PREFIX}}-public-b"
  }
}
resource "aws_subnet" "public_c" {
  vpc_id     = "${aws_vpc.{{PREFIX}}.id}"
  cidr_block = "10.{{OCTET}}.32.0/20"
  availability_zone = "ap-southeast-1c"
  tags = {
    Name = "{{PREFIX}}-public-c"
  }
}
resource "aws_subnet" "private_a" {
  vpc_id     = "${aws_vpc.{{PREFIX}}.id}"
  cidr_block = "10.{{OCTET}}.64.0/20"
  availability_zone = "ap-southeast-1a"
  tags = {
    Name = "{{PREFIX}}-private-a"
  }
}
resource "aws_subnet" "private_b" {
  vpc_id     = "${aws_vpc.{{PREFIX}}.id}"
  cidr_block = "10.{{OCTET}}.128.0/20"
  availability_zone = "ap-southeast-1b"
  tags = {
    Name = "{{PREFIX}}-private-b"
  }
}
resource "aws_subnet" "private_c" {
  vpc_id     = "${aws_vpc.{{PREFIX}}.id}"
  cidr_block = "10.{{OCTET}}.144.0/20"
  availability_zone = "ap-southeast-1c"
  tags = {
    Name = "{{PREFIX}}-private-c"
  }
}



resource "aws_internet_gateway" "gw" {
  vpc_id = "${aws_vpc.{{PREFIX}}.id}"
  tags = {
    Name = "{{PREFIX}}-int-gw"
  }

}

resource "aws_eip" "nat_gw" {
  vpc      = true
  tags = {
    Name = "{{PREFIX}}-nat-gw"
  }

}

resource "aws_nat_gateway" "gw" {
  allocation_id = "${aws_eip.nat_gw.id}"
  subnet_id     = "${aws_subnet.public_a.id}"
  tags = {
    Name = "{{PREFIX}}-nat-gw"
  }
}

