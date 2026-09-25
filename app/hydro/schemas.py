from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class WellCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=50)
    name: str = Field(..., min_length=1, max_length=120)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    aquifer: str = Field(..., min_length=1, max_length=120)
    screen_depth_m: float = Field(..., gt=0, le=5000)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class EndmemberCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    isotope_d18o: float = Field(..., ge=-100, le=100)
    isotope_d2h: float = Field(..., ge=-800, le=800)
    solute_mg_l: float = Field(..., ge=0, le=100000)
    uncertainty: float = Field(default=0.1, gt=0, le=100)
    version: str = Field(default="v1", min_length=1, max_length=40)


class SampleCreate(BaseModel):
    sample_code: str = Field(..., min_length=3, max_length=64)
    sampled_at: str = Field(..., min_length=20, max_length=40)
    isotope_d18o: float | None = Field(default=None, ge=-100, le=100)
    isotope_d2h: float | None = Field(default=None, ge=-800, le=800)
    solute_mg_l: float | None = Field(default=None, ge=0, le=100000)
    detection_limit: float = Field(default=0, ge=0, le=100000)
    measurement_error: float = Field(default=0.05, ge=0, le=100)


# ---- 参数集版本（草稿/复核/发布/撤销） -------------------------------------


class ParameterEndmemberInput(BaseModel):
    code: str = Field(..., min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=100)
    isotope_d18o: float = Field(..., ge=-100, le=100)
    isotope_d2h: float = Field(..., ge=-800, le=800)
    solute_mg_l: float = Field(..., ge=0, le=100000)
    uncertainty: float = Field(default=0.1, gt=0, le=100)


class ParameterSetDraft(BaseModel):
    porosity: float = Field(..., gt=0, lt=1)
    velocity_m_day: float = Field(..., gt=0, le=10000)
    dispersion_m2_day: float = Field(..., gt=0, le=100000)
    endmembers: list[ParameterEndmemberInput] = Field(..., min_length=2, max_length=8)
    change_note: str = Field(default="", max_length=500)

    @field_validator("endmembers")
    @classmethod
    def unique_endmember_codes(cls, value: list[ParameterEndmemberInput]) -> list[ParameterEndmemberInput]:
        codes = [item.code.strip() for item in value]
        if len(set(codes)) != len(codes):
            raise ValueError("端元编码不能重复")
        return value


class ParameterSetRevise(BaseModel):
    porosity: float | None = Field(default=None, gt=0, lt=1)
    velocity_m_day: float | None = Field(default=None, gt=0, le=10000)
    dispersion_m2_day: float | None = Field(default=None, gt=0, le=100000)
    endmembers: list[ParameterEndmemberInput] | None = Field(default=None, min_length=2, max_length=8)
    change_note: str | None = Field(default=None, max_length=500)
    # 乐观锁：客户端必须回传当前草稿的内容哈希，服务端据此检测并发改写
    base_hash: str = Field(..., min_length=8, max_length=80)

    @field_validator("endmembers")
    @classmethod
    def unique_endmember_codes(cls, value: list[ParameterEndmemberInput] | None) -> list[ParameterEndmemberInput] | None:
        if value is not None and len({item.code.strip() for item in value}) != len(value):
            raise ValueError("端元编码不能重复")
        return value


class PublishRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class RevokeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class RecalculateRequest(BaseModel):
    kinds: list[Literal["inversion", "transport"]] | None = None


class InversionRequest(BaseModel):
    parameter_set_id: int = Field(..., ge=1)
    method: str = Field(default="weighted-least-squares", pattern="^(weighted-least-squares|projected-gradient)$")
    max_iterations: int = Field(default=500, ge=10, le=10000)
    tolerance: float = Field(default=1e-8, gt=0, le=0.1)
    model_version: str = Field(default="mix-1", min_length=1, max_length=40)


class TransportRequest(BaseModel):
    parameter_set_id: int = Field(..., ge=1)
    source_concentration: float = Field(..., ge=0, le=1000000)
    distance_m: float = Field(..., gt=0, le=1000000)
    decay_per_day: float = Field(default=0, ge=0, le=100)
    duration_days: float = Field(..., gt=0, le=100000)
    step_days: float = Field(default=1, gt=0, le=1000)
    model_version: str = Field(default="ade-1", min_length=1, max_length=40)


