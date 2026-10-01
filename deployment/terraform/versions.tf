terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Uncomment after the first local apply, or point at an existing bucket.
  # backend "s3" {
  #   bucket         = "classeasily-terraform-state"
  #   key            = "prod/terraform.tfstate"
  #   region         = "us-east-2"
  #   dynamodb_table = "classeasily-terraform-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "classeasily"
      Environment = "shared"
      ManagedBy   = "terraform"
    }
  }
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "classeasily"
      Environment = "shared"
      ManagedBy   = "terraform"
    }
  }
}
