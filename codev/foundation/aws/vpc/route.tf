resource "aws_route_table" "public" {
  vpc_id = "${aws_vpc.{{PREFIX}}.id}"

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = "${aws_internet_gateway.gw.id}"
  }

  tags = {
    Name = "{{PREFIX}}-rt-public"
  }
}

resource "aws_route_table" "private" {
  vpc_id = "${aws_vpc.{{PREFIX}}.id}"

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = "${aws_nat_gateway.gw.id}"
  }

  tags = {
    Name = "{{PREFIX}}-rt-private"
  }
}

resource "aws_route_table_association" "public_rt_subnet_a" {
  subnet_id      = "${aws_subnet.public_a.id}"
  route_table_id = "${aws_route_table.public.id}"
}
resource "aws_route_table_association" "public_rt_subnet_b" {
  subnet_id      = "${aws_subnet.public_b.id}"
  route_table_id = "${aws_route_table.public.id}"
}
resource "aws_route_table_association" "public_rt_subnet_c" {
  subnet_id      = "${aws_subnet.public_c.id}"
  route_table_id = "${aws_route_table.public.id}"
}

resource "aws_route_table_association" "private_rt_subnet_a" {
  subnet_id      = "${aws_subnet.private_a.id}"
  route_table_id = "${aws_route_table.private.id}"
}
resource "aws_route_table_association" "private_rt_subnet_b" {
  subnet_id      = "${aws_subnet.private_b.id}"
  route_table_id = "${aws_route_table.private.id}"
}
resource "aws_route_table_association" "private_rt_subnet_c" {
  subnet_id      = "${aws_subnet.private_c.id}"
  route_table_id = "${aws_route_table.private.id}"
}

