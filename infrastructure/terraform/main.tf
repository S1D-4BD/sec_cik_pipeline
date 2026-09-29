terraform {
    required_providers {
        aws = {
            source  = "hashicorp/aws"
            version = "~> 5.0"
        }
    }
}

# pour se connecter 
provider "aws" {
    region = "us-east-2" # ou se connecter et stocker mes trucs
}

# creer la ressource dans le cloud 
resource "aws_s3_bucket" "sec_filings" { # aws_s3_bucket = le type, "sec_filings = nom"
    bucket_prefix = "sec-filings" # les noms auront la forme sec-filings-data-20260901140021-x8z...
    force_destroy = true # utorise Terraform a tout supprimer (bucket + fichiers à l'intérieur) si un jour on execute terraform destroy
}

output "bucket_name" {
    value = aws_s3_bucket.sec_filings.bucket
    description = "name of the S3 bucket created for SEC filings"
}