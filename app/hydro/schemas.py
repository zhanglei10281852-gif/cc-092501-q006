from __future__ import annotations

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


class EndmemberSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    isotope_d18o: float = Field(..., ge=-100, le=100)
    isotope_d2h: float = Field(..., ge=-800, le=800)
    solute_mg_l: float = Field(..., ge=0, le=100000)
    uncertainty: float = Field(default=0.1, gt=0, le=100)
    source_endmember_id: int | None = Field(default=None, ge=1)


class ParameterSetCreate(BaseModel):
    site_code: str = Field(..., min_length=2, max_length=50)
    code: str = Field(..., min_length=1, max_length=50)
    porosity: float = Field(..., gt=0, le=1)
    velocity_m_day: float = Field(..., gt=0, le=10000)
    dispersion_m2_day: float = Field(..., gt=0, le=100000)
    decay_per_day: float = Field(default=0, ge=0, le=100)
    notes: str = Field(default="", max_length=2000)
    endmembers: list[EndmemberSpec] = Field(..., min_length=1, max_length=8)

    @field_validator("site_code", "code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("endmembers")
    @classmethod
    def unique_endmember_names(cls, value: list[EndmemberSpec]) -> list[EndmemberSpec]:
        names = [item.name.strip() for item in value]
        if len(set(names)) != len(names):
            raise ValueError("端元名称在参数集内必须唯一")
        return value


class ParameterSetUpdate(BaseModel):
    porosity: float | None = Field(default=None, gt=0, le=1)
    velocity_m_day: float | None = Field(default=None, gt=0, le=10000)
    dispersion_m2_day: float | None = Field(default=None, gt=0, le=100000)
    decay_per_day: float | None = Field(default=None, ge=0, le=100)
    notes: str | None = Field(default=None, max_length=2000)
    endmembers: list[EndmemberSpec] | None = Field(default=None, min_length=1, max_length=8)

    @field_validator("endmembers")
    @classmethod
    def unique_endmember_names(cls, value: list[EndmemberSpec] | None) -> list[EndmemberSpec] | None:
        if value is None:
            return value
        names = [item.name.strip() for item in value]
        if len(set(names)) != len(names):
            raise ValueError("端元名称在参数集内必须唯一")
        return value


class ReviewDecision(BaseModel):
    comment: str = Field(default="", max_length=2000)


class PublishRequest(BaseModel):
    # 客户端发布时声明自己看到的基准版本，用于并发发布的旧版本冲突检测；首个版本传 null
    expected_base_version_id: int | None = Field(default=None, ge=1)


class RetractRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class RecomputeRequest(BaseModel):
    task_types: list[str] = Field(default_factory=lambda: ["inversion", "transport"])
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("task_types")
    @classmethod
    def known_task_types(cls, value: list[str]) -> list[str]:
        allowed = {"inversion", "transport"}
        unknown = [item for item in value if item not in allowed]
        if unknown:
            raise ValueError(f"未知的任务类型：{','.join(unknown)}")
        return sorted(set(value))


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
    duration_days: float = Field(..., gt=0, le=100000)
    step_days: float = Field(default=1, gt=0, le=1000)
    model_version: str = Field(default="ade-1", min_length=1, max_length=40)
